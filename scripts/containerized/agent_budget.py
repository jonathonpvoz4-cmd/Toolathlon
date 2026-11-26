"""Cancel only the agent on timeout and await its normal save/cleanup path."""
import asyncio

from utils.api_model.interrupted_stream import agent_budget_state


async def run_with_budget(operation, seconds):
    state = {'expired': False}
    token = agent_budget_state.set(state)
    task = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait([task], timeout=max(0.001, seconds))
        if done:
            return await task
        state['expired'] = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise asyncio.TimeoutError('Agent execution budget exhausted')
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        agent_budget_state.reset(token)
