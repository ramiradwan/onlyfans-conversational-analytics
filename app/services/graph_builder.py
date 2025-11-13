"""  
Graph Builder service.  
  
Converts enriched conversation data into LPG vertices & edges,  
ready for insertion into Cosmos DB / Gremlin API.  
  
Supports:  
- Full graph rebuild from snapshot  
- Incremental append from delta  
- Accepting enrichment results as ConversationNode (or ExtendedConversationNode)  
"""  
  
from typing import List, Dict  
from datetime import datetime  
  
from app.models.graph import (  
    Fan,  
    Creator,  
    ConversationNode,  
    Topic,  
    EngagementAction,  
    InteractionOutcome,  
    GraphEdge,  
)  
from app.utils.logger import logger  
from app.utils.normalization import normalize_str, normalize_datetime  
from app.utils.time import utcnow  
from app.utils.ws_errors import broadcast_system_error  
  
# In-memory placeholder for per-user graph state (replace with DB integration)  
_user_graphs: Dict[str, Dict[str, List]] = {}  
  
  
class GraphBuilder:  
    """Low-level vertex/edge construction methods for LPG schema."""  
  
    def __init__(self, creator_id: str):  
        self.creator_id = normalize_str(creator_id)  
  
    def build_fan_vertex(  
        self,  
        fan_id: str,  
        join_date: datetime = None,  
        demographics: Dict = None,  
        sentiment: float = None  
    ) -> Fan:  
        return Fan(  
            fanId=normalize_str(fan_id),  
            joinDate=normalize_datetime(join_date),  
            demographics=demographics or {},  
            sentimentProfile=sentiment  
        )  
  
    def build_creator_vertex(self, niche: str = None, style_profile: Dict = None) -> Creator:  
        return Creator(  
            creatorId=self.creator_id,  
            niche=normalize_str(niche),  
            styleProfile=style_profile or {}  
        )  
  
    def build_conversation_vertex(  
        self,  
        conversation_id: str,  
        start_date: datetime,  
        end_date: datetime,  
        message_count: int,  
        avg_response_time: float,  
        turns: int,  
        silence_pct: float  
    ) -> ConversationNode:  
        safe_start = normalize_datetime(start_date) or utcnow()  
        safe_end = normalize_datetime(end_date) or safe_start  
        return ConversationNode(  
            conversationId=normalize_str(conversation_id),  
            startDate=safe_start,  
            endDate=safe_end,  
            messageCount=message_count,  
            averageResponseTime=avg_response_time,  
            turns=turns,  
            silencePercentage=silence_pct  
        )  
  
    def _ensure_models(self, items: List, model_cls) -> List:  
        """Ensure all items are instances of the given Pydantic model."""  
        result = []  
        safe_items = items or []  
        for item in safe_items:  
            try:  
                if isinstance(item, model_cls):  
                    result.append(item)  
                elif isinstance(item, dict):  
                    result.append(model_cls.model_validate(item))  
                else:  
                    raise TypeError(f"Expected {model_cls.__name__} or dict, got {type(item)}")  
            except Exception as e:  
                logger.exception(f"[GRAPH] Failed to validate {model_cls.__name__} item: {e}")  
                continue  
        return result  
  
    def build_edges(  
        self,  
        fan: Fan,  
        conversation: ConversationNode,  
        topics: List[Topic],  
        actions: List[EngagementAction],  
        outcomes: List[InteractionOutcome]  
    ) -> List[GraphEdge]:  
        edges: List[GraphEdge] = []  
  
        # Fan has conversation  
        edges.append(GraphEdge(  
            from_id=fan.fanId,  
            to_id=conversation.conversationId,  
            label="HAS_CONVERSATION",  
            properties={"attendance": "Participated"}  
        ))  
  
        # Conversation discusses topics  
        for topic in topics or []:  
            edges.append(GraphEdge(  
                from_id=conversation.conversationId,  
                to_id=topic.topicId,  
                label="DISCUSS_TOPIC",  
                properties={}  
            ))  
  
        # Conversation uses engagement actions  
        for action in actions or []:  
            edges.append(GraphEdge(  
                from_id=conversation.conversationId,  
                to_id=action.actionId,  
                label="USES_ENGAGEMENT",  
                properties={}  
            ))  
  
        # Actions target topics  
        for action in actions or []:  
            for topic in topics or []:  
                edges.append(GraphEdge(  
                    from_id=action.actionId,  
                    to_id=topic.topicId,  
                    label="TARGETS_TOPIC",  
                    properties={"rationale": "Placeholder"}  
                ))  
  
        # Conversation results in outcomes  
        for outcome in outcomes or []:  
            edges.append(GraphEdge(  
                from_id=conversation.conversationId,  
                to_id=outcome.outcomeId,  
                label="RESULTS_IN_OUTCOME",  
                properties={"changeFromPrevious": None}  
            ))  
  
        return edges  
  
    def build_graph(  
        self,  
        enriched_conv: ConversationNode,  
        fan_id: str  
    ) -> Dict[str, List]:  
        """  
        Build vertices and edges from enriched conversation data.  
        Accepts ConversationNode or subclass (ExtendedConversationNode).  
        """  
        try:  
            logger.info(  
                f"[GRAPH] Building graph for conversation {enriched_conv.conversationId} "  
                f"(creator={self.creator_id}, fan={fan_id})"  
            )  
  
            # Vertices  
            fan_vertex = self.build_fan_vertex(  
                fan_id,  
                sentiment=getattr(enriched_conv, "sentiment", None)  
            )  
            creator_vertex = self.build_creator_vertex()  
  
            # Dates with safe fallback  
            start_date = enriched_conv.startDate or (  
                enriched_conv.messages[0].createdAt if enriched_conv.messages else None  
            )  
            end_date = enriched_conv.endDate or (  
                enriched_conv.messages[-1].createdAt if enriched_conv.messages else None  
            )  
            start_date = normalize_datetime(start_date) or utcnow()  
            end_date = normalize_datetime(end_date) or start_date  
  
            conversation_vertex = self.build_conversation_vertex(  
                conversation_id=enriched_conv.conversationId,  
                start_date=start_date,  
                end_date=end_date,  
                message_count=len(enriched_conv.messages or []),  
                avg_response_time=enriched_conv.averageResponseTime,  
                turns=enriched_conv.turns,  
                silence_pct=enriched_conv.silencePercentage  
            )  
  
            # Related entities  
            topics = self._ensure_models(enriched_conv.topics or [], Topic)  
            actions = self._ensure_models(enriched_conv.actions or [], EngagementAction)  
            outcomes = self._ensure_models(enriched_conv.outcomes or [], InteractionOutcome)  
  
            vertices = [fan_vertex, creator_vertex, conversation_vertex] + topics + actions + outcomes  
            edges = self.build_edges(fan_vertex, conversation_vertex, topics, actions, outcomes)  
  
            logger.debug(  
                f"[GRAPH] Graph built for conv={enriched_conv.conversationId}: "  
                f"{len(vertices)} vertices, {len(edges)} edges"  
            )  
            return {"vertices": vertices, "edges": edges}  
  
        except Exception as e:  
            logger.exception(  
                f"[GRAPH] Failed to build graph for conversation {getattr(enriched_conv, 'conversationId', 'unknown')}: {e}"  
            )  
            return {"vertices": [], "edges": []}  
  
  
# ------------------------------------------------------------------  
# Top-level async functions  
# ------------------------------------------------------------------  
  
async def rebuild_graph_from_snapshot(  
    user_id: str,  
    enriched_conversations: List[ConversationNode],  
    creator_id: str  
) -> None:  
    """Rebuild the user's entire graph from a full snapshot."""  
    logger.info(f"[GRAPH] Rebuilding graph from snapshot for {user_id} (creator={creator_id})")  
    builder = GraphBuilder(creator_id=creator_id)  
  
    _user_graphs[user_id] = {"vertices": [], "edges": []}  
  
    for enriched_data in enriched_conversations:  
        try:  
            fan_id = getattr(enriched_data.withUser, "id", None) or "fan_unknown"  
            graph = builder.build_graph(enriched_data, fan_id)  
            _user_graphs[user_id]["vertices"].extend(graph["vertices"])  
            _user_graphs[user_id]["edges"].extend(graph["edges"])  
            logger.debug(  
                f"[GRAPH] Added conv {enriched_data.conversationId}: "  
                f"{len(graph['vertices'])} vertices, {len(graph['edges'])} edges"  
            )  
        except Exception as e:  
            logger.exception(f"[GRAPH] Failed to process conversation for {user_id}: {e}")  
            await broadcast_system_error(user_id, "graph_snapshot_failed", str(e))  
            continue  
  
    logger.info(  
        f"[GRAPH] Snapshot graph built for {user_id}: "  
        f"{len(_user_graphs[user_id]['vertices'])} vertices, "  
        f"{len(_user_graphs[user_id]['edges'])} edges"  
    )  
  
  
async def append_graph_from_delta(  
    user_id: str,  
    enriched_conversation: ConversationNode,  
    creator_id: str  
) -> None:  
    """Append new data from an enriched delta conversation to the user's graph."""  
    logger.info(  
        f"[GRAPH] Appending delta to graph for {user_id}: conv={enriched_conversation.conversationId} (creator={creator_id})"  
    )  
    builder = GraphBuilder(creator_id=creator_id)  
  
    try:  
        fan_id = getattr(enriched_conversation.withUser, "id", None) or "fan_unknown"  
        graph = builder.build_graph(enriched_conversation, fan_id)  
  
        if user_id not in _user_graphs:  
            _user_graphs[user_id] = {"vertices": [], "edges": []}  
  
        _user_graphs[user_id]["vertices"].extend(graph["vertices"])  
        _user_graphs[user_id]["edges"].extend(graph["edges"])  
  
        logger.debug(  
            f"[GRAPH] Delta appended for conv={enriched_conversation.conversationId}: "  
            f"{len(graph['vertices'])} vertices, {len(graph['edges'])} edges"  
        )  
    except Exception as e:  
        logger.exception(  
            f"[GRAPH] Failed to append delta for {user_id} (conv={getattr(enriched_conversation, 'conversationId', 'unknown')}): {e}"  
        )  
        await broadcast_system_error(user_id, "graph_delta_failed", str(e))  