import asyncio
from langgraph_sdk import get_client


async def main():
    client = get_client(url="http://localhost:8111")

    assistant = await client.assistants.get(assistant_id="fe096781-5601-53d2-b2f6-0d3403f7e9ca")
    thread = await client.threads.create()

    async for chunk in client.runs.stream(
        thread_id=thread["thread_id"],
        assistant_id=assistant["assistant_id"],
        input={"messages": [{"type": "human", "content": "Hello!"}]},
    ):
        print(chunk)


if __name__ == "__main__":
    asyncio.run(main())
