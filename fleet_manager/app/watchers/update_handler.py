import logging

from app.services.webhook_sender import WebhookSender
from app.core.config import get_settings
from app.db.redis_client import peer_cache_set


logger = logging.getLogger(__name__)


class UpdateHandler:
    async def handle_new_message(
        self, client, account_id: int, message, db, redis_conn, webhook_url: str = None
    ):
        from app.db.models import GlobalPeer
        from sqlalchemy import select

        settings = get_settings()

        from_peer_id = message.from_user.id
        chat_id = message.chat.id
        message_id = message.id
        text = message.text or ""

        try:
            resolved_from = await client.resolve_peer(from_peer_id)
            if resolved_from:
                stmt = select(GlobalPeer).where(GlobalPeer.peer_id == from_peer_id)
                result = await db.execute(stmt)
                peer = result.scalar_one_or_none()

                if not peer:
                    peer = GlobalPeer(
                        peer_id=from_peer_id,
                        username=getattr(message.from_user, "username", None),
                        first_name=getattr(message.from_user, "first_name", None),
                        last_name=getattr(message.from_user, "last_name", None),
                    )
                    db.add(peer)
                    await db.flush()

                await peer_cache_set(redis_conn, str(from_peer_id), from_peer_id)

            resolved_chat = await client.resolve_peer(chat_id)
            if resolved_chat:
                await peer_cache_set(redis_conn, str(chat_id), chat_id)

            payload = {
                "event": "incoming_message",
                "account_id": account_id,
                "from_peer_id": from_peer_id,
                "chat_id": chat_id,
                "message": text,
                "message_id": message_id,
                "date": message.date.isoformat() if message.date else None,
            }

            target_url = webhook_url or settings.N8N_SYSTEM_WEBHOOK_URL
            await WebhookSender().send(delivery_id=0, url=target_url, payload=payload)

            logger.info(
                f"message_webhook_sent account={account_id} from={from_peer_id}"
            )

        except Exception as e:
            logger.error(f"handle_message_error: {e}")
            raise
