"""  
Graph Builder service.  
  
Converts enriched conversation data into LPG vertices & edges,  
ready for insertion into Cosmos DB / Gremlin API.  
  
Refactored to support:  
- Full graph rebuild from snapshot  
- Incremental append from delta  
"""  
  
from typing import List, Dict  
from datetime import datetime  
  
from app.models.core import ChatThread, Message  
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
  
# In-memory placeholder for per-user graph state (replace with DB integration)  
_user_graphs: Dict[str, Dict[str, List]] = {}  
  
  
class GraphBuilder:  
    """Low-level vertex/edge construction methods."""  
  
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
        safe_end = normalize_datetime(end_date)  
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
        result = []  
        for item in items:  
            if isinstance(item, model_cls):  
                result.append(item)  
            elif isinstance(item, dict):  
                result.append(model_cls.model_validate(item))  
            else:  
                raise TypeError(f"Expected {model_cls.__name__} or dict, got {type(item)}")  
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
        edges.append(  
            GraphEdge(  
                from_id=fan.fanId,  
                to_id=conversation.conversationId,  
                label="HAS_CONVERSATION",  
                properties={"attendance": "Participated"}  
            )  
        )  
        for topic in topics:  
            edges.append(  
                GraphEdge(  
                    from_id=conversation.conversationId,  
                    to_id=topic.topicId,  
                    label="DISCUSS_TOPIC",  
                    properties={}  
                )  
            )  
        for action in actions:  
            edges.append(  
                GraphEdge(  
                    from_id=conversation.conversationId,  
                    to_id=action.actionId,  
                    label="USES_ENGAGEMENT",  
                    properties={}  
                )  
            )  
        for action in actions:  
            for topic in topics:  
                edges.append(  
                    GraphEdge(  
                        from_id=action.actionId,  
                        to_id=topic.topicId,  
                        label="TARGETS_TOPIC",  
                        properties={"rationale": "Placeholder"}  
                    )  
                )  
        for outcome in outcomes:  
            edges.append(  
                GraphEdge(  
                    from_id=conversation.conversationId,  
                    to_id=outcome.outcomeId,  
                    label="RESULTS_IN_OUTCOME",  
                    properties={"changeFromPrevious": None}  
                )  
            )  
        return edges  
  
    def build_graph(self, enriched_conv: dict, fan_id: str) -> Dict[str, List]:  
        """  
        Build vertices and edges from already enriched conversation data.  
        """  
        logger.info(  
            f"Building graph for conversation {enriched_conv.get('conversationId')} and fan {fan_id}"  
        )  
  
        fan_vertex = self.build_fan_vertex(fan_id, sentiment=enriched_conv.get("sentiment"))  
        creator_vertex = self.build_creator_vertex()  
  
        start_date = enriched_conv.get("startDate") or None  
        end_date = enriched_conv.get("endDate") or None  
  
        msgs = enriched_conv.get("messages") or []  
        if not start_date and msgs:  
            start_date = getattr(msgs[0], "created_at", None) or msgs[0].get("created_at")  
        if not end_date and msgs:  
            end_date = getattr(msgs[-1], "created_at", None) or msgs[-1].get("created_at")  
  
        conversation_vertex = self.build_conversation_vertex(  
            conversation_id=enriched_conv.get("conversationId"),  
            start_date=start_date,  
            end_date=end_date,  
            message_count=len(enriched_conv.get("actions", [])),  
            avg_response_time=None,  
            turns=None,  
            silence_pct=None  
        )  
  
        topics = self._ensure_models(enriched_conv.get("topics", []), Topic)  
        actions = self._ensure_models(enriched_conv.get("actions", []), EngagementAction)  
        outcomes = self._ensure_models(enriched_conv.get("outcomes", []), InteractionOutcome)  
  
        vertices = [fan_vertex, creator_vertex, conversation_vertex] + topics + actions + outcomes  
        edges = self.build_edges(fan_vertex, conversation_vertex, topics, actions, outcomes)  
  
        logger.debug(f"Graph built: {len(vertices)} vertices, {len(edges)} edges")  
        return {"vertices": vertices, "edges": edges}  
  
  
# -----------------------------  
# Top-level stateless functions  
# -----------------------------  
  
def rebuild_graph_from_snapshot(user_id: str, enriched_conversations: List[dict]) -> None:  
    """  
    Rebuilds the user's entire graph from a full snapshot of enriched conversations.  
    """  
    logger.info(f"[GRAPH] Rebuilding graph from snapshot for {user_id}")  
    builder = GraphBuilder(creator_id="creator_demo")  # TODO: real creator_id  
  
    # Reset graph state for user  
    _user_graphs[user_id] = {"vertices": [], "edges": []}  
  
    for enriched_data in enriched_conversations:  
        fan_id = enriched_data.get("fanId") or "fan_unknown"  
        graph = builder.build_graph(enriched_data, fan_id)  
        _user_graphs[user_id]["vertices"].extend(graph["vertices"])  
        _user_graphs[user_id]["edges"].extend(graph["edges"])  
  
    logger.info(  
        f"[GRAPH] Snapshot graph built for {user_id}: "  
        f"{len(_user_graphs[user_id]['vertices'])} vertices, "  
        f"{len(_user_graphs[user_id]['edges'])} edges"  
    )  
  
  
def append_graph_from_delta(user_id: str, enriched_conversation: dict) -> None:  
    """  
    Appends new data from an enriched delta conversation to the user's graph.  
    """  
    logger.info(f"[GRAPH] Appending delta to graph for {user_id}: conv {enriched_conversation.get('conversationId')}")  
    builder = GraphBuilder(creator_id="creator_demo")  # TODO: real creator_id  
  
    fan_id = enriched_conversation.get("fanId") or "fan_unknown"  
    graph = builder.build_graph(enriched_conversation, fan_id)  
  
    if user_id not in _user_graphs:  
        _user_graphs[user_id] = {"vertices": [], "edges": []}  
    _user_graphs[user_id]["vertices"].extend(graph["vertices"])  
    _user_graphs[user_id]["edges"].extend(graph["edges"])  
  
    logger.debug(  
        f"[GRAPH] Delta appended for {user_id}: "  
        f"{len(graph['vertices'])} vertices, {len(graph['edges'])} edges"  
    )  