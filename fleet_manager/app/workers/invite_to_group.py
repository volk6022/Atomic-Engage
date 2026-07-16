from app.workers.base_task import run_task


async def invite_to_group(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            group = payload.get("group_username") or payload.get("group")
            user = payload.get("user_peer_id") or payload.get("user_username")
            chat = await client.add_chat_members(group, user)
            return {"chat_id": getattr(chat, "id", None), "group": group, "user": user}

        return action

    return await run_task(ctx, task_id, builder)
