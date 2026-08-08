import logging

from app.core.config import get_settings
from app.services.webhook_queue import enqueue_webhook
from app.db.redis_client import peer_cache_set, seen_message_setnx
from app.watchers._message_payload import build_incoming_message_payload


logger = logging.getLogger(__name__)


class UpdateHandler:
    async def handle_new_message(
        self, client, account_id: int, message, db, redis_conn, webhook_url: str = None
    ):
        """Forward a qualifying incoming update to n8n (Constitution — n8n Contract).

        `db` is the **session_maker** (matches app.watchers.watcher_process): a fresh
        per-message session is opened here so concurrent updates on a long-lived Watcher
        client never share a session (Constitution I). Channel broadcast posts
        (from_user is None) are handled — the source falls back to sender_chat/chat via
        `build_incoming_message_payload` — so news/job-channel posts are forwarded rather
        than dropped. Duplicate (chat_id, message_id) forwards are suppressed (US3).
        """
        from app.db.models import GlobalPeer
        from sqlalchemy import select

        settings = get_settings()

        payload = build_incoming_message_payload(account_id, message)
        chat_id = payload["chat_id"]
        message_id = payload["message_id"]

        try:
            # Dedup FIRST — a duplicate must cost nothing and never re-forward.
            if redis_conn is not None and chat_id is not None and message_id is not None:
                first_seen = await seen_message_setnx(redis_conn, chat_id, message_id)
                if not first_seen:
                    logger.debug(
                        f"duplicate_message_skipped account={account_id} "
                        f"chat={chat_id} msg={message_id}"
                    )
                    return

            async with db() as session:
                from_user = getattr(message, "from_user", None)

                # Passively persist a real sending user (peerdata, Constitution I).
                # Channel/anonymous posts have no user — nothing to upsert.
                if from_user is not None:
                    from_peer_id = from_user.id
                    resolved_from = await client.resolve_peer(from_peer_id)
                    if resolved_from:
                        stmt = select(GlobalPeer).where(
                            GlobalPeer.peer_id == from_peer_id
                        )
                        peer = (await session.execute(stmt)).scalar_one_or_none()
                        if not peer:
                            peer = GlobalPeer(
                                peer_id=from_peer_id,
                                username=getattr(from_user, "username", None),
                                first_name=getattr(from_user, "first_name", None),
                                last_name=getattr(from_user, "last_name", None),
                            )
                            session.add(peer)
                            await session.flush()
                        if redis_conn is not None:
                            await peer_cache_set(
                                redis_conn, str(from_peer_id), from_peer_id
                            )

                # Cache the chat peer too (channel/group/user) for later resolution.
                if chat_id is not None:
                    resolved_chat = await client.resolve_peer(chat_id)
                    if resolved_chat and redis_conn is not None:
                        await peer_cache_set(redis_conn, str(chat_id), chat_id)

                # Queue the forward inside the same session: delivery is durable before
                # this returns, but the HTTP call is not awaited here. Inline delivery
                # retried for 450 s, which on a busy discussion group would have stalled
                # the update handler behind one unreachable receiver.
                target_url = webhook_url or settings.N8N_SYSTEM_WEBHOOK_URL
                await enqueue_webhook(session, target_url, payload)

            logger.info(
                f"message_webhook_sent account={account_id} chat={chat_id} "
                f"channel_post={payload['is_channel_post']}"
            )

        except Exception as e:
            logger.error(f"handle_message_error: {e}")
            raise
