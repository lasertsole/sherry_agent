import asyncio

from server.service.messages import _get_agent_history_list

async def main():
    print(await _get_agent_history_list("main"))

if __name__ == "__main__":
    asyncio.run(main())
