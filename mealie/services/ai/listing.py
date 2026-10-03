from collections.abc import AsyncIterable, AsyncIterator

MAX_LISTED_MODELS = 1000
"""
Most models read from a provider's model list. The SDKs follow pagination for as long as the server says there is
more, so a base URL pointing somewhere hostile could otherwise keep a request listing forever.
"""


async def take[T](items: AsyncIterable[T], limit: int) -> AsyncIterator[T]:
    """The first `limit` items of `items`, without reading any further"""
    if limit <= 0:
        return

    count = 0
    async for item in items:
        yield item
        count += 1
        if count >= limit:
            return
