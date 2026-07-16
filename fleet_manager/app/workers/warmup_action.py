from app.workers.base_task import run_task


async def warmup_action(ctx, task_id: int) -> dict:
    """Internal-only warmup step. For now performs a lightweight, non-outbound client
    touch (get_me) and records the action; tier advancement is handled in US6."""

    def builder(payload):
        action_name = (payload or {}).get("action", "profile_view")

        async def action(client):
            await client.get_me()
            return {"action": action_name}

        return action

    return await run_task(ctx, task_id, builder)
