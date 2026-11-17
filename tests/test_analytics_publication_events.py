import asyncio

import pytest
from fastapi import HTTPException

from app.analytics.publication_events import PublicationEvents
from app.api.endpoints.insights import AnalyticsPublicationResponse


@pytest.mark.asyncio
async def test_publication_stream_is_account_scoped_and_reconciles_on_connect():
    events = PublicationEvents(maximum=2)
    allowed = True

    def authorize():
        if not allowed:
            raise HTTPException(401)

    stream = AnalyticsPublicationResponse("creator-1", authorize, events=events)
    stream.listener = events.subscribe("creator-1")
    other = events.subscribe("creator-2")
    assert events.subscribe("creator-3") is None
    frames = stream.frames()
    try:
        assert await anext(frames) == "event: analytics-changed\ndata: {}\n\n"
        events.publish("creator-2")
        await asyncio.sleep(0)
        assert other[2].is_set()
        assert not stream.listener[2].is_set()
        waiting = asyncio.create_task(anext(frames))
        await asyncio.sleep(0)
        events.publish("creator-1")
        assert await asyncio.wait_for(waiting, 2) == "event: analytics-changed\ndata: {}\n\n"
        waiting = asyncio.create_task(anext(frames))
        allowed = False
        events.publish("creator-1")
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiting, 2)
    finally:
        await frames.aclose()
        events.unsubscribe(stream.listener)
        events.unsubscribe(other)
    assert events.subscribe("creator-3") is not None


@pytest.mark.asyncio
async def test_publication_response_releases_subscription_when_transport_closes():
    events = PublicationEvents(maximum=1)
    response = AnalyticsPublicationResponse("creator-1", lambda: None, events=events)
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        raise OSError("transport closed")
    with pytest.raises(Exception):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert events.subscribe("creator-2") is not None
