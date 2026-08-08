from app.workers.base_task import humanize_typing_visible, run_task


async def send_message(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            # kurigram resolves a username string or numeric id as the chat target.
            target = payload.get("recipient_username") or payload.get("peer_id")
            # Human pacing WITH a visible "typing…" indicator (FR-330-333): the typing
            # delay runs here, inside the connected window, rather than off-line in
            # _humanize_before, so the recipient actually sees the account typing.
            await humanize_typing_visible(client, target, payload["text"])
            msg = await client.send_message(
                target, payload["text"], reply_to_message_id=payload.get("reply_to_message_id")
            )
            return {"telegram_message_id": getattr(msg, "id", None), "target": target}

        return action

    return await run_task(ctx, task_id, builder)
