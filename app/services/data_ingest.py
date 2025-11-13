"""  
Race-condition safe data ingestion service.  
  
Buffers incoming deltas until the initial snapshot is processed,  
then applies them in order. Uses Redis Pub/Sub to broadcast updates  
to frontend clients.  
  
Spec compliance:  
 - Implements snapshot-then-delta ingestion  
 - Per-user asyncio.Queue buffers deltas until snapshot ready  
 - Broadcasts OutgoingWssMessage types to frontend (Bridge) via Redis  
"""  
  
import asyncio  
from datetime import datetime  
from typing import Dict, List  
  
from app.models.core import ChatThread, Message, SystemStatus, UserRef  
from app.models.ingest import CacheUpdatePayload, NewRawMessagePayload  
from app.models.insights import FullSyncResponse, AnalyticsUpdate  
from app.models.graph import ExtendedConversationNode  
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
from app.utils.ws_errors import broadcast_system_error  
  
  
class DataIngestService:  
    """Dual-mode data ingestion: snapshot first, then deltas."""  
  
    def __init__(self) -> None:  
        self._cache: Dict[str, Dict[str, List[dict]]] = {}  
        self._delta_queues: Dict[str, asyncio.Queue] = {}  
        self._snapshot_ready: Dict[str, bool] = {}  
  
    # ------------------------------------------------------------------  
    # Internal helpers  
    # ------------------------------------------------------------------  
  
    def _ensure_user_structs(self, user_id: str) -> None:  
        """Initialize per-user cache, queue, and snapshot-ready flag."""  
        if user_id not in self._cache:  
            self._cache[user_id] = {"chats": [], "messages": []}  
        if user_id not in self._delta_queues:  
            self._delta_queues[user_id] = asyncio.Queue()  
        if user_id not in self._snapshot_ready:  
            self._snapshot_ready[user_id] = False  
  
    async def _broadcast_status(self, user_id: str, status: str) -> None:  
        """Send a system_status message to the frontend."""  
        try:  
            status_msg = SystemStatusMsg(  
                type="system_status",  
                payload=SystemStatus(status=status)  
            )  
            await broadcast.publish(  
                channel=f"frontend_user_{user_id}",  
                message=status_msg.model_dump_json()  
            )  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Broadcast status failed for {user_id}: {e}")  
  
    @staticmethod  
    def _inject_analytics(node: ExtendedConversationNode, analytics: AnalyticsUpdate) -> None:  
        """Inject analytics fields (priorityScore, unread_count) into a node."""  
        if analytics.priorityScores:  
            node.priorityScore = analytics.priorityScores.get(node.conversationId)  
        if analytics.unreadCounts and node.withUser:  
            node.withUser.extra["unread_count"] = analytics.unreadCounts.get(node.conversationId)  
  
    # ------------------------------------------------------------------  
    # Public ingestion methods  
    # ------------------------------------------------------------------  
  
    async def handle_snapshot(self, user_id: str, payload: dict, creator_id: str) -> None:  
        """  
        Process full snapshot from extension.  
  
        Steps:  
        1. Validate payload  
        2. Store in cache  
        3. Broadcast PROCESSING_SNAPSHOT  
        4. Enrich conversations  
        5. Broadcast enrichment results  
        6. Rebuild graph  
        7. Build analytics + inject into conversations  
        8. Broadcast full_sync_response  
        9. Mark snapshot ready  
        10. Drain delta queue  
        11. Broadcast REALTIME  
        """  
        self._ensure_user_structs(user_id)  
  
        # Validate payload  
        try:  
            snapshot = CacheUpdatePayload.model_validate(payload)  
        except Exception as e:  
            logger.error(f"[DATA_INGEST] Invalid snapshot payload for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "invalid_snapshot_payload", str(e))  
            return  
  
        # Cache raw snapshot  
        self._cache[user_id]["chats"] = [c.model_dump() for c in snapshot.chats]  
        self._cache[user_id]["messages"] = [m.model_dump() for m in snapshot.messages]  
  
        logger.info(  
            f"[DATA_INGEST] Snapshot stored for {user_id}, creator={creator_id}: "  
            f"{len(snapshot.chats)} chats, {len(snapshot.messages)} messages"  
        )  
  
        await self._broadcast_status(user_id, "PROCESSING_SNAPSHOT")  
  
        # Enrich conversations  
        try:  
            service = EnrichmentService()  
            enriched_convs: List[ExtendedConversationNode] = [  
                service.enrich_conversation(chat, snapshot.messages)  
                for chat in snapshot.chats  
            ]  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Enrichment failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "enrichment_failed", str(e))  
            return  
  
        # Broadcast enrichment results (pass creator_id for logging)  
        try:  
            await broadcast_enrichment(user_id, snapshot.chats, snapshot.messages, creator_id)  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Broadcast enrichment failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "enrichment_broadcast_failed", str(e))  
  
        # Rebuild graph  
        try:  
            await rebuild_graph_from_snapshot(user_id, enriched_convs, creator_id)  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Graph rebuild failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "graph_rebuild_failed", str(e))  
            return  
  
        # Build analytics & inject into nodes (pass creator_id)  
        try:  
            analytics_payload = await insights_service.build_analytics_update(user_id, None, None, creator_id)  
            for conv in enriched_convs:  
                self._inject_analytics(conv, analytics_payload)  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Analytics build failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "analytics_build_failed", str(e))  
            analytics_payload = AnalyticsUpdate(topics=[], sentiment_trend=None, response_time_metrics=None)  
  
        # Broadcast full_sync_response  
        try:  
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
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Broadcast full_sync_response failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "full_sync_broadcast_failed", str(e))  
  
        # Mark snapshot ready  
        self._snapshot_ready[user_id] = True  
  
        # ✅ FIXED: Remove unsupported "SNAPSHOT_READY" literal per spec  
        # Drain delta queue before switching to REALTIME  
        try:  
            await self._process_delta_queue(user_id, creator_id)  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Delta queue processing failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "delta_queue_failed", str(e))  
  
        # Now broadcast REALTIME status  
        await self._broadcast_status(user_id, "REALTIME")  
  
    async def handle_delta(self, user_id: str, payload: dict, creator_id: str) -> None:  
        """Handle a new_raw_message delta from Agent."""  
        self._ensure_user_structs(user_id)  
  
        try:  
            delta = NewRawMessagePayload.model_validate(payload)  
        except Exception as e:  
            logger.error(f"[DATA_INGEST] Invalid delta payload for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "invalid_delta_payload", str(e))  
            return  
  
        if not self._snapshot_ready[user_id]:  
            await self._delta_queues[user_id].put(delta)  
            logger.debug(  
                f"[DATA_INGEST] Queued delta for {user_id}, creator={creator_id} "  
                f"(snapshot not ready, queue size={self._delta_queues[user_id].qsize()})"  
            )  
            return  
  
        await self._apply_delta(user_id, delta, creator_id)  
  
    # ------------------------------------------------------------------  
    # Delta processing  
    # ------------------------------------------------------------------  
  
    async def _process_delta_queue(self, user_id: str, creator_id: str) -> None:  
        """Process all queued deltas after snapshot is ready."""  
        queue = self._delta_queues[user_id]  
        logger.info(f"[DATA_INGEST] Processing {queue.qsize()} queued deltas for {user_id}, creator={creator_id}")  
        while not queue.empty():  
            delta = await queue.get()  
            await self._apply_delta(user_id, delta, creator_id)  
  
    async def _apply_delta(self, user_id: str, delta: NewRawMessagePayload, creator_id: str) -> None:  
        """Append delta to cache, update graph, broadcast append_message."""  
        logger.debug(  
            f"[DATA_INGEST] Applying delta for {user_id}, creator={creator_id}: "  
            f"message_id={delta.message.id}, chat_id={delta.message.chat_id}, "  
            f"createdAt={getattr(delta.message, 'createdAt', None)}"  
        )  
  
        # Append to cache  
        self._cache[user_id]["messages"].append(delta.message.model_dump())  
  
        # Enrichment  
        try:  
            service = EnrichmentService()  
            chat_stub = ChatThread(  
                id=str(delta.message.chat_id),  
                messages=[delta.message],  
                withUser=delta.message.fromUser  
            )  
            enriched_node = service.enrich_conversation(chat_stub, [delta.message])  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Delta enrichment failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "delta_enrichment_failed", str(e))  
            return  
  
        try:  
            await broadcast_enrichment(user_id, [chat_stub], [delta.message], creator_id)  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Delta enrichment broadcast failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "delta_enrichment_broadcast_failed", str(e))  
  
        try:  
            await append_graph_from_delta(user_id, enriched_node, creator_id)  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Append to graph failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "graph_append_failed", str(e))  
  
        try:  
            analytics_payload = await insights_service.build_analytics_update(user_id, None, None, creator_id)  
            self._inject_analytics(enriched_node, analytics_payload)  
            analytics_msg = AnalyticsUpdateMsg(  
                type="analytics_update",  
                payload=analytics_payload  
            )  
            await broadcast.publish(  
                channel=f"frontend_user_{user_id}",  
                message=analytics_msg.model_dump_json()  
            )  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Delta analytics broadcast failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "delta_analytics_failed", str(e))  
  
        try:  
            append_msg = AppendMessageMsg(  
                type="append_message",  
                payload=enriched_node  
            )  
            await broadcast.publish(  
                channel=f"frontend_user_{user_id}",  
                message=append_msg.model_dump_json()  
            )  
        except Exception as e:  
            logger.exception(f"[DATA_INGEST] Append_message broadcast failed for {user_id}, creator={creator_id}: {e}")  
            await broadcast_system_error(user_id, "append_message_failed", str(e))  
  
    # ------------------------------------------------------------------  
    # Cache accessors  
    # ------------------------------------------------------------------  
  
    def get_cached_chats(self, user_id: str) -> List[ChatThread]:  
        """Return cached chats for a user as validated models."""  
        chats_raw = self._cache.get(user_id, {}).get("chats", [])  
        return [ChatThread.model_validate(c) for c in chats_raw]  
  
    def get_cached_messages(self, user_id: str) -> List[Message]:  
        """Return cached messages for a user as validated models."""  
        msgs_raw = self._cache.get(user_id, {}).get("messages", [])  
        return [Message.model_validate(m) for m in msgs_raw]  