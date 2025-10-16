# Build LPG objects for Cosmos DB Gremlin

"""  
Graph Builder:  
Takes enriched conversation data → Produces LPG vertices & edges  
ready for insertion into Cosmos DB / Gremlin API.  
"""  
  
from typing import List, Dict  
from app.models.graph import (  
    Fan, Creator, ConversationNode, Topic,  
    EngagementAction, InteractionOutcome, GraphEdge  
)  
  
  
class GraphBuilder:  
    """  
    Converts enriched conversation data into graph vertices and edges  
    following the creator-fan LPG schema.  
    """  
  
    def __init__(self, creator_id: str):  
        self.creator_id = creator_id  
  
    def build_fan_vertex(self, fan_id: str, join_date=None, demographics=None, sentiment=None) -> Fan:  
        return Fan(  
            fanId=fan_id,  
            joinDate=join_date,  
            demographics=demographics or {},  
            sentimentProfile=sentiment  
        )  
  
    def build_creator_vertex(self, niche=None, style_profile=None) -> Creator:  
        return Creator(  
            creatorId=self.creator_id,  
            niche=niche,  
            styleProfile=style_profile or {}  
        )  
  
    def build_conversation_vertex(self, conversation_id: str, start_date, end_date,  
                                  message_count: int, avg_response_time: float,  
                                  turns: int, silence_pct: float) -> ConversationNode:  
        return ConversationNode(  
            conversationId=conversation_id,  
            startDate=start_date,  
            endDate=end_date,  
            messageCount=message_count,  
            averageResponseTime=avg_response_time,  
            turns=turns,  
            silencePercentage=silence_pct  
        )  
  
    def _ensure_models(self, items, model_cls):  
        """  
        Ensure every item in items is a Pydantic model instance of type model_cls.  
        Convert dicts into model_cls instances if needed.  
        """  
        result = []  
        for item in items:  
            if isinstance(item, model_cls):  
                result.append(item)  
            elif isinstance(item, dict):  
                result.append(model_cls.model_validate(item))  
            else:  
                raise TypeError(f"Expected {model_cls.__name__} or dict, got {type(item)}")  
        return result  
  
    def build_edges(self,  
                    fan: Fan,  
                    conversation: ConversationNode,  
                    topics: List[Topic],  
                    actions: List[EngagementAction],  
                    outcomes: List[InteractionOutcome]) -> List[GraphEdge]:  
        edges: List[GraphEdge] = []  
  
        # Fan → Conversation  
        edges.append(GraphEdge(  
            from_id=fan.fanId,  
            to_id=conversation.conversationId,  
            label="HAS_CONVERSATION",  
            properties={"attendance": "Participated"}  
        ))  
  
        # Conversation → Topics  
        for topic in topics:  
            edges.append(GraphEdge(  
                from_id=conversation.conversationId,  
                to_id=topic.topicId,  
                label="DISCUSS_TOPIC",  
                properties={}  
            ))  
  
        # Conversation → Engagement Actions  
        for action in actions:  
            edges.append(GraphEdge(  
                from_id=conversation.conversationId,  
                to_id=action.actionId,  
                label="USES_ENGAGEMENT",  
                properties={}  
            ))  
  
        # Engagement Action → Topic  
        for action in actions:  
            for topic in topics:  
                edges.append(GraphEdge(  
                    from_id=action.actionId,  
                    to_id=topic.topicId,  
                    label="TARGETS_TOPIC",  
                    properties={"rationale": "Placeholder"}  
                ))  
  
        # Conversation → Outcomes  
        for outcome in outcomes:  
            edges.append(GraphEdge(  
                from_id=conversation.conversationId,  
                to_id=outcome.outcomeId,  
                label="RESULTS_IN_OUTCOME",  
                properties={"changeFromPrevious": None}  
            ))  
  
        return edges  
  
    def build_graph(self, enriched_conv: dict, fan_id: str) -> Dict[str, List]:  
        """  
        Given enriched conversation data and fan ID,  
        produce vertices and edges lists (all Pydantic models).  
        """  
        fan_vertex = self.build_fan_vertex(fan_id, sentiment=enriched_conv["sentiment"])  
        creator_vertex = self.build_creator_vertex()  
  
        conversation_vertex = self.build_conversation_vertex(  
            conversation_id=enriched_conv["conversationId"],  
            start_date=None,  # TODO: fill from raw data  
            end_date=None,    # TODO: fill from raw data  
            message_count=len(enriched_conv.get("actions", [])),  
            avg_response_time=None,  
            turns=None,  
            silence_pct=None  
        )  
  
        topics = self._ensure_models(enriched_conv.get("topics", []), Topic)  
        actions = self._ensure_models(enriched_conv.get("actions", []), EngagementAction)  
        outcomes = self._ensure_models(enriched_conv.get("outcomes", []), InteractionOutcome)  
  
        vertices = (  
            [fan_vertex, creator_vertex, conversation_vertex]  
            + topics  
            + actions  
            + outcomes  
        )  
  
        edges = self.build_edges(  
            fan_vertex,  
            conversation_vertex,  
            topics,  
            actions,  
            outcomes  
        )  
  
        return {"vertices": vertices, "edges": edges}  