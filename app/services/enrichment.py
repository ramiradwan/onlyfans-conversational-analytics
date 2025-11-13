"""  
Enrichment pipeline:  
Processes raw conversations/messages → Extracts semantic + behavioral features →  
Produces structured enrichment objects for graph building and analytics.  
"""  
  
from typing import List, Optional  
from datetime import datetime  
  
from app.models.core import Message, ChatThread  
from app.models.graph import (  
    Topic,  
    EngagementAction,  
    InteractionOutcome,  
    EnrichmentResultPayload,  
    ExtendedConversationNode,  
)  
from app.utils.logger import logger  
from app.utils.normalization import normalize_str, normalize_datetime  
from app.core.broadcast import broadcast  
from app.models.wss import EnrichmentResultMsg  
  
  
class EnrichmentService:  
    """  
    Processes raw conversation data and extracts:  
      - Topics (NER, keyword clustering, embeddings)  
      - Engagement actions (message type classification)  
      - Sentiment analysis  
      - Interaction outcomes (tips, renewals, drop-offs)  
    """  
  
    def __init__(self, nlp_model=None, sentiment_model=None, embedding_model=None):  
        self.nlp_model = nlp_model  
        self.sentiment_model = sentiment_model  
        self.embedding_model = embedding_model  
  
    def extract_topics(self, messages: List[Message]) -> List[Topic]:  
        try:  
            logger.debug(f"[ENRICHMENT] Extracting topics from {len(messages)} messages")  
            return [  
                Topic(  
                    topicId="topic_demo",  
                    description="Placeholder topic",  
                    embedding=[0.0, 0.1, 0.2],  
                    category="General",  
                )  
            ]  
        except Exception as e:  
            logger.exception(f"[ENRICHMENT] extract_topics failed: {e}")  
            return []  
  
    def classify_engagement_actions(self, messages: List[Message]) -> List[EngagementAction]:  
        try:  
            logger.debug(f"[ENRICHMENT] Classifying engagement actions from {len(messages)} messages")  
            return [  
                EngagementAction(  
                    actionId="action_demo",  
                    name="Send Text",  
                    embedding=[0.1, 0.2, 0.3],  
                    type="text",  
                )  
            ]  
        except Exception as e:  
            logger.exception(f"[ENRICHMENT] classify_engagement_actions failed: {e}")  
            return []  
  
    def analyze_sentiment(self, messages: List[Message]) -> float:  
        try:  
            logger.debug(f"[ENRICHMENT] Analyzing sentiment for {len(messages)} messages")  
            return 0.75  # Placeholder  
        except Exception as e:  
            logger.exception(f"[ENRICHMENT] analyze_sentiment failed: {e}")  
            return 0.0  
  
    def detect_interaction_outcomes(self, messages: List[Message]) -> List[InteractionOutcome]:  
        try:  
            logger.debug(f"[ENRICHMENT] Detecting interaction outcomes for {len(messages)} messages")  
            return [  
                InteractionOutcome(  
                    outcomeId="outcome_demo",  
                    name="Positive Sentiment",  
                    score=0.75,  
                )  
            ]  
        except Exception as e:  
            logger.exception(f"[ENRICHMENT] detect_interaction_outcomes failed: {e}")  
            return []  
  
    def enrich_conversation(  
        self,  
        conversation: ChatThread,  
        all_messages: Optional[List[Message]] = None  
    ) -> ExtendedConversationNode:  
        """  
        Enrich a conversation and return a spec-compliant ExtendedConversationNode.  
        If all_messages is provided, it's used to augment the conversation.messages list.  
        """  
        try:  
            conv_id = conversation.id or getattr(conversation, "conversationId", None) or "unknown"  
            logger.info(f"[ENRICHMENT] Enriching conversation {conv_id}")  
  
            # Select messages  
            messages = conversation.messages or []  
            if all_messages is not None:  
                merged = [m for m in all_messages if str(m.chat_id) == str(conv_id)]  
                if merged:  
                    logger.debug(f"[ENRICHMENT] Using {len(merged)} merged messages from all_messages for conv {conv_id}")  
                    messages = merged  
  
            # Extract enrichment features  
            topics = self.extract_topics(messages) or []  
            actions = self.classify_engagement_actions(messages) or []  
            sentiment = self.analyze_sentiment(messages) or 0.0  
            outcomes = self.detect_interaction_outcomes(messages) or []  
  
            logger.debug(  
                f"[ENRICHMENT] Enriched conv {conv_id}: "  
                f"{len(topics)} topics, {len(actions)} actions, "  
                f"sentiment={sentiment}, {len(outcomes)} outcomes"  
            )  
  
            # Safe start & end dates  
            safe_start = None  
            if messages:  
                safe_start = getattr(messages[0], "createdAt", None) or getattr(messages[0], "created_at", None)  
            safe_start = normalize_datetime(safe_start) or datetime.utcnow()  
  
            safe_end = None  
            if messages:  
                safe_end = getattr(messages[-1], "createdAt", None) or getattr(messages[-1], "created_at", None)  
            safe_end = normalize_datetime(safe_end) or safe_start  
  
            # Build ExtendedConversationNode  
            enriched_node = ExtendedConversationNode(  
                conversationId=normalize_str(conv_id),  
                startDate=safe_start,  
                endDate=safe_end,  
                messageCount=len(messages),  
                averageResponseTime=None,  
                turns=None,  
                silencePercentage=None,  
                messages=messages,  
                topics=topics,  
                actions=actions,  
                sentiment=sentiment,  
                outcomes=outcomes,  
                priorityScore=None,  # placeholder for analytics scoring  
                withUser=conversation.withUser  
            )  
  
            return enriched_node  
  
        except Exception as e:  
            logger.exception(f"[ENRICHMENT] enrich_conversation failed for {getattr(conversation, 'id', None)}: {e}")  
            return ExtendedConversationNode(  
                conversationId=normalize_str(getattr(conversation, 'id', None) or "unknown"),  
                startDate=datetime.utcnow(),  
                endDate=None,  
                messageCount=0,  
                averageResponseTime=None,  
                turns=None,  
                silencePercentage=None,  
                messages=[],  
                topics=[],  
                actions=[],  
                sentiment=0.0,  
                outcomes=[],  
                priorityScore=None,  
                withUser=conversation.withUser  
            )  
  
  
# ----------------------------  
# Module-level helper  
# ----------------------------  
  
def enrich_conversation(  
    conversation: ChatThread,  
    all_messages: Optional[List[Message]] = None  
) -> ExtendedConversationNode:  
    """Stateless enrichment function used by graph_builder and ingest service."""  
    service = EnrichmentService()  
    return service.enrich_conversation(conversation, all_messages)  
  
  
# ----------------------------  
# Spec-compliant broadcaster hook  
# ----------------------------  
  
async def broadcast_enrichment(  
    user_id: str,  
    conversations: List[ChatThread],  
    all_messages: Optional[List[Message]] = None,  
    creator_id: Optional[str] = None  
) -> None:  
    """  
    Enrich one or more conversations and broadcast enrichment_result messages  
    to the frontend via Redis.  
    """  
    service = EnrichmentService()  
  
    for conv in conversations:  
        try:  
            enriched_node = service.enrich_conversation(conv, all_messages)  
  
            enrichment_msg = EnrichmentResultMsg(  
                type="enrichment_result",  
                payload=EnrichmentResultPayload(  
                    # ✅ FIXED: camelCase field name to match model spec  
                    conversationId=enriched_node.conversationId,  
                    topics=enriched_node.topics or [],  
                    actions=enriched_node.actions or [],  
                    sentiment=enriched_node.sentiment if enriched_node.sentiment is not None else 0.0,  
                    outcomes=enriched_node.outcomes or []  
                )  
            )  
  
            await broadcast.publish(  
                channel=f"frontend_user_{user_id}",  
                message=enrichment_msg.model_dump_json()  
            )  
  
            logger.info(  
                f"[ENRICHMENT] Broadcast enrichment for user={user_id}, creator={creator_id or 'unknown'}, "  
                f"conversation={conv.id} ({len(enriched_node.topics)} topics, {len(enriched_node.actions)} actions, "  
                f"sentiment={enriched_node.sentiment}, {len(enriched_node.outcomes)} outcomes)"  
            )  
  
        except Exception as e:  
            logger.exception(  
                f"[ENRICHMENT] Failed to broadcast enrichment for conversation={conv.id}, user={user_id}: {e}"  
            )  
            continue  