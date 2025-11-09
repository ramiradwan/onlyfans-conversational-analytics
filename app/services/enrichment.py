"""  
Enrichment pipeline:  
Processes raw conversations/messages → Extracts semantic + behavioral features →  
Produces structured enrichment objects for graph building and analytics.  
"""  
  
from typing import List, Optional  
from app.models.core import Message, ChatThread  
from app.models.graph import Topic, EngagementAction, InteractionOutcome, EnrichmentResultPayload  
from app.utils.logger import logger  
from app.utils.normalization import normalize_str  
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
        logger.debug(f"Extracting topics from {len(messages)} messages")  
        return [  
            Topic(  
                topicId="topic_demo",  
                description="Placeholder topic",  
                embedding=[0.0, 0.1, 0.2],  
                category="General"  
            )  
        ]  
  
    def classify_engagement_actions(self, messages: List[Message]) -> List[EngagementAction]:  
        logger.debug(f"Classifying engagement actions from {len(messages)} messages")  
        return [  
            EngagementAction(  
                actionId="action_demo",  
                name="Send Text",  
                embedding=[0.1, 0.2, 0.3],  
                type="text"  
            )  
        ]  
  
    def analyze_sentiment(self, messages: List[Message]) -> float:  
        logger.debug(f"Analyzing sentiment for {len(messages)} messages")  
        return 0.75  # Placeholder  
  
    def detect_interaction_outcomes(self, messages: List[Message]) -> List[InteractionOutcome]:  
        logger.debug(f"Detecting interaction outcomes for {len(messages)} messages")  
        return [  
            InteractionOutcome(  
                outcomeId="outcome_demo",  
                name="Positive Sentiment",  
                score=0.75  
            )  
        ]  
  
    def enrich_conversation(self, conversation: ChatThread, all_messages: Optional[List[Message]] = None) -> dict:  
        """  
        Enrich a conversation with topics, actions, sentiment, and outcomes.  
        If all_messages is provided, it's used to augment the conversation.messages list.  
        """  
        logger.info(f"Enriching conversation {conversation.id}")  
  
        messages = conversation.messages  
        if all_messages is not None:  
            # Optionally merge with full message list for more context  
            messages = [m for m in all_messages if str(m.chat_id) == str(conversation.id)]  
  
        topics = self.extract_topics(messages)  
        actions = self.classify_engagement_actions(messages)  
        sentiment = self.analyze_sentiment(messages)  
        outcomes = self.detect_interaction_outcomes(messages)  
  
        enriched = {  
            "conversationId": normalize_str(conversation.id),  
            "fanId": getattr(conversation.withUser, "id", None) or "fan_unknown",  
            "messages": messages,  
            "topics": topics,  
            "actions": actions,  
            "sentiment": sentiment,  
            "outcomes": outcomes  
        }  
  
        logger.debug(  
            f"Enriched conversation {conversation.id}: "  
            f"{len(topics)} topics, {len(actions)} actions, "  
            f"sentiment={sentiment}, {len(outcomes)} outcomes"  
        )  
        return enriched  
  
  
# ----------------------------  
# Module-level helper  
# ----------------------------  
  
def enrich_conversation(conversation: ChatThread, all_messages: Optional[List[Message]] = None) -> dict:  
    """  
    Stateless enrichment function used by graph_builder.  
    """  
    service = EnrichmentService()  
    return service.enrich_conversation(conversation, all_messages)  
  
  
# ----------------------------  
# Spec-compliant broadcaster hook  
# ----------------------------  
  
async def broadcast_enrichment(user_id: str, conversations: List[ChatThread], all_messages: Optional[List[Message]] = None) -> None:  
    """  
    Enrich one or more conversations and broadcast enrichment_result messages to the frontend via Redis.  
    """  
    service = EnrichmentService()  
  
    for conv in conversations:  
        enriched = service.enrich_conversation(conv, all_messages)  
  
        enrichment_msg = EnrichmentResultMsg(  
            type="enrichment_result",  
            payload=EnrichmentResultPayload(  
                conversation_id=enriched["conversationId"],  
                topics=enriched["topics"],  
                actions=enriched["actions"],  
                sentiment=enriched["sentiment"],  
                outcomes=enriched["outcomes"]  
            )  
        )  
  
        await broadcast.publish(  
            channel=f"frontend_user_{user_id}",  
            message=enrichment_msg.model_dump_json()  
        )  
  
        logger.info(f"[ENRICHMENT] Broadcast enrichment for user {user_id}, conversation {conv.id}")  