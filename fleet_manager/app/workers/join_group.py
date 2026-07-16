from app.workers.base_task import run_task


async def join_group(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            target = payload.get("invite_link") or payload.get("target")
            chat = await client.join_chat(target)
            return {"chat_id": getattr(chat, "id", None), "target": target}

        return action

    return await run_task(ctx, task_id, builder)
