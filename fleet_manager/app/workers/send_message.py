from app.workers.base_task import run_task


async def send_message(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            # kurigram resolves a username string or numeric id as the chat target.
            target = payload.get("recipient_username") or payload.get("peer_id")
            msg = await client.send_message(
                target, payload["text"], reply_to_message_id=payload.get("reply_to_message_id")
            )
            return {"telegram_message_id": getattr(msg, "id", None), "target": target}

        return action

    return await run_task(ctx, task_id, builder)
