from app.workers.base_task import run_task


async def react(ctx, task_id: int) -> dict:
    def builder(payload):
        async def action(client):
            await client.send_reaction(
                payload["peer_id"], payload["message_id"], [payload.get("reaction", "👍")]
            )
            return {"peer_id": payload["peer_id"], "message_id": payload["message_id"]}

        return action

    return await run_task(ctx, task_id, builder)
