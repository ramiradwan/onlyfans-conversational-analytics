"""  
Race-condition safe data ingestion service.  
  
Buffers incoming deltas until the initial snapshot is processed,  
then applies them in order. Uses Redis Pub/Sub to broadcast updates  
to frontend clients.  
"""  
  
import asyncio  
from typing import Dict, List  
  
from pydantic import TypeAdapter  
  
from app.models.core import ChatThread, Message, SystemStatus  
from app.models.ingest import CacheUpdatePayload, NewRawMessagePayload  
from app.models.insights import FullSyncResponse, AnalyticsUpdate  
from app.models.wss import (  
    FullSyncResponseMsg,  
    AppendMessageMsg,  
    SystemStatusMsg,  
    AnalyticsUpdateMsg,  
)  
from app.services.graph_builder import rebuild_graph_from_snapshot, append_graph_from_delta  
from app.services.enrichment import EnrichmentService, broadcast_enrichment  
from app.services import insights_service  
from app.utils.logger import logger  
from app.core.broadcast import broadcast  
  
# Type adapters  
cache_update_adapter = TypeAdapter(CacheUpdatePayload)  
new_raw_msg_adapter = TypeAdapter(NewRawMessagePayload)  
  
  
class DataIngestService:  
    def __init__(self) -> None:  
        # Per-user cache of latest chats/messages  
        self._cache: Dict[str, Dict[str, List[dict]]] = {}  
        # Per-user asyncio.Queue for deltas  
        self._delta_queues: Dict[str, asyncio.Queue] = {}  
        # Track whether snapshot processed for user  
        self._snapshot_ready: Dict[str, bool] = {}  
  
    def _ensure_user_structs(self, user_id: str) -> None:  
        if user_id not in self._cache:  
            self._cache[user_id] = {"chats": [], "messages": []}  
        if user_id not in self._delta_queues:  
            self._delta_queues[user_id] = asyncio.Queue()  
        if user_id not in self._snapshot_ready:  
            self._snapshot_ready[user_id] = False  
  
    async def handle_snapshot(self, user_id: str, payload: dict) -> None:  
        """Process full snapshot from extension."""  
        self._ensure_user_structs(user_id)  
  
        try:  
            snapshot = cache_update_adapter.validate_python(payload)  
        except Exception as e:  
            logger.error(f"[DATA_INGEST] Invalid snapshot payload for {user_id}: {e}")  
            return  
  
        # Store raw cache  
        self._cache[user_id]["chats"] = [c.model_dump() for c in snapshot.chats]  
        self._cache[user_id]["messages"] = [m.model_dump() for m in snapshot.messages]  
        self._snapshot_ready[user_id] = True  
  
        logger.info(  
            f"[DATA_INGEST] Snapshot stored for {user_id}: "  
            f"{len(snapshot.chats)} chats, {len(snapshot.messages)} messages"  
        )  
  
        await self._broadcast_status(user_id, "PROCESSING_SNAPSHOT")  
  
        # Step 1–2: Enrich all conversations  
        service = EnrichmentService()  
        enriched_convs = []  
        for chat in snapshot.chats:  
            enriched = service.enrich_conversation(chat, snapshot.messages)  
            enriched_convs.append(enriched)  
  
        # Step 3: Broadcast enrichment results  
        await broadcast_enrichment(user_id, snapshot.chats, snapshot.messages)  
  
        # Step 4: Rebuild graph from enriched conversations  
        rebuild_graph_from_snapshot(user_id, enriched_convs)  
  
        # Step 5: Build analytics payload for snapshot  
        topics = await insights_service.fetch_topic_metrics(None, None)  
        sentiment_trend = await insights_service.fetch_sentiment_trend(None, None)  
        response_time_metrics = await insights_service.fetch_response_time_metrics(None, None)  
  
        analytics_payload = AnalyticsUpdate(  
            topics=topics,  
            sentiment_trend=sentiment_trend,  
            response_time_metrics=response_time_metrics  
        )  
  
        # Step 6: Broadcast full_sync_response with both conversations & analytics  
        full_sync_payload = FullSyncResponse(  
            conversations=enriched_convs,  
            analytics=analytics_payload  
        )  
  
        full_sync_msg = FullSyncResponseMsg(  
            type="full_sync_response",  
            payload=full_sync_payload  
        )  
  
        await broadcast.publish(  
            channel=f"frontend_user_{user_id}",  
            message=full_sync_msg.model_dump_json()  
        )  
  
        # Drain delta queue  
        await self._process_delta_queue(user_id)  
  
        await self._broadcast_status(user_id, "REALTIME")  
  
    async def handle_delta(self, user_id: str, payload: dict) -> None:  
        """Handle a new_raw_message delta."""  
        self._ensure_user_structs(user_id)  
  
        try:  
            delta = new_raw_msg_adapter.validate_python(payload)  
        except Exception as e:  
            logger.error(f"[DATA_INGEST] Invalid delta payload for {user_id}: {e}")  
            return  
  
        if not self._snapshot_ready[user_id]:  
            await self._delta_queues[user_id].put(delta)  
            logger.debug(f"[DATA_INGEST] Queued delta for {user_id} (snapshot not ready)")  
            return  
  
        await self._apply_delta(user_id, delta)  
  
    async def _process_delta_queue(self, user_id: str) -> None:  
        """Process all queued deltas after snapshot is ready."""  
        queue = self._delta_queues[user_id]  
        while not queue.empty():  
            delta = await queue.get()  
            await self._apply_delta(user_id, delta)  
  
    async def _apply_delta(self, user_id: str, delta: NewRawMessagePayload) -> None:  
        """Append delta to cache, update graph, broadcast append_message."""  
        self._cache[user_id]["messages"].append(delta.message.model_dump())  
  
        # Step 1–2: Enrich just this conversation  
        service = EnrichmentService()  
        # In a real implementation, look up the full ChatThread  
        chat_stub = ChatThread(  
            id=str(delta.message.chat_id),  
            messages=[delta.message],  
            withUser={"id": "fan_unknown"}  
        )  
        enriched = service.enrich_conversation(chat_stub, [delta.message])  
  
        # Step 3: Broadcast enrichment for this conversation  
        await broadcast_enrichment(user_id, [chat_stub], [delta.message])  
  
        # Step 4: Append to graph from enriched data  
        append_graph_from_delta(user_id, enriched)  
  
        # Step 5: Broadcast updated analytics (delta mode)  
        topics = await insights_service.fetch_topic_metrics(None, None)  
        sentiment_trend = await insights_service.fetch_sentiment_trend(None, None)  
        response_time_metrics = await insights_service.fetch_response_time_metrics(None, None)  
  
        analytics_payload = AnalyticsUpdate(  
            topics=topics,  
            sentiment_trend=sentiment_trend,  
            response_time_metrics=response_time_metrics  
        )  
  
        analytics_msg = AnalyticsUpdateMsg(  
            type="analytics_update",  
            payload=analytics_payload  
        )  
  
        await broadcast.publish(  
            channel=f"frontend_user_{user_id}",  
            message=analytics_msg.model_dump_json()  
        )  
  
        # Step 6: Broadcast append_message  
        append_msg = AppendMessageMsg(  
            type="append_message",  
            payload=delta.message  
        )  
        await broadcast.publish(  
            channel=f"frontend_user_{user_id}",  
            message=append_msg.model_dump_json()  
        )  
  
    async def _broadcast_status(self, user_id: str, status: str) -> None:  
        """Helper to send system_status messages."""  
        status_msg = SystemStatusMsg(  
            type="system_status",  
            payload=SystemStatus(status=status)  
        )  
        await broadcast.publish(  
            channel=f"frontend_user_{user_id}",  
            message=status_msg.model_dump_json()  
        )  
  
    def get_cached_chats(self, user_id: str) -> List[ChatThread]:  
        chats_raw = self._cache.get(user_id, {}).get("chats", [])  
        return [ChatThread.model_validate(c) for c in chats_raw]  
  
    def get_cached_messages(self, user_id: str) -> List[Message]:  
        msgs_raw = self._cache.get(user_id, {}).get("messages", [])  
        return [Message.model_validate(m) for m in msgs_raw]  