"""  
Insights Service:  
Executes Gremlin traversals against Azure Cosmos DB to compute analytics metrics.  
"""  
  
from datetime import datetime, timedelta  
from typing import Optional, List, Dict, Tuple  
  
from app.models.insights import (  
    TopicMetricsResponse,  
    SentimentTrendResponse,  
    SentimentTrendPoint,  
    ResponseTimeMetricsResponse,  
    AnalyticsUpdate,  
    FullSyncResponse,  
)  
from app.models.graph import ExtendedConversationNode  
from app.models.wss import AnalyticsUpdateMsg  
from app.core.broadcast import broadcast  
from app.utils.logger import logger  
from app.utils.normalization import normalize_datetime  
from app.utils.ws_errors import broadcast_system_error  
  
# ----------------------------  
# Utility helpers  
# ----------------------------  
  
def _default_date_range(  
    start_date: Optional[datetime],  
    end_date: Optional[datetime]  
) -> Tuple[datetime, datetime]:  
    """Return safe default date range if inputs are None."""  
    if not start_date and not end_date:  
        end = datetime.utcnow()  
        start = end - timedelta(days=30)  
        return start, end  
    if not start_date:  
        start_date = datetime.utcnow() - timedelta(days=30)  
    if not end_date:  
        end_date = datetime.utcnow()  
    return start_date, end_date  
  
  
def _normalize_creator_id(creator_id: Optional[str]) -> Optional[str]:  
    """Return None if creator_id is falsy, else strip whitespace."""  
    if creator_id and creator_id.strip():  
        return creator_id.strip()  
    return None  
  
# ----------------------------  
# Analytics fetch methods  
# ----------------------------  
  
async def fetch_topic_metrics(  
    start_date: Optional[datetime],  
    end_date: Optional[datetime],  
    creator_id: Optional[str] = None  
) -> List[TopicMetricsResponse]:  
    """Aggregate topics discussed within a date range."""  
    start_date, end_date = _default_date_range(start_date, end_date)  
    start_dt = normalize_datetime(start_date)  
    end_dt = normalize_datetime(end_date)  
    creator_id = _normalize_creator_id(creator_id)  
  
    logger.info(f"[INSIGHTS] Fetching topic metrics for creator={creator_id or 'ALL'} from {start_dt} to {end_dt}")  
  
    try:  
        # TODO: Replace with actual Gremlin query filtered by creator_id:  
        # g.V().hasLabel('ConversationNode')  
        #      .has('creatorId', creator_id)  # apply if creator_id is not None  
        #      .has('date', between(start_dt, end_dt))  
        #      .out('DISCUSS_TOPIC').groupCount()...  
        result = [  
            TopicMetricsResponse(  
                topic="Example Topic",  
                volume=42,  
                percentage_of_total=12.5,  
                trend=0.8  
            )  
        ]  
        logger.debug(f"[INSIGHTS] Topic metrics count: {len(result)}")  
        return result  
    except Exception as e:  
        logger.exception(f"[INSIGHTS] fetch_topic_metrics failed: {e}")  
        return []  
  
  
async def fetch_sentiment_trend(  
    start_date: Optional[datetime],  
    end_date: Optional[datetime],  
    creator_id: Optional[str] = None  
) -> SentimentTrendResponse:  
    """Compute sentiment trend over time."""  
    start_date, end_date = _default_date_range(start_date, end_date)  
    start_dt = normalize_datetime(start_date)  
    end_dt = normalize_datetime(end_date)  
    creator_id = _normalize_creator_id(creator_id)  
  
    logger.info(f"[INSIGHTS] Fetching sentiment trend for creator={creator_id or 'ALL'} from {start_dt} to {end_dt}")  
  
    try:  
        # TODO: Replace with actual Gremlin query filtered by creator_id:  
        # g.V().hasLabel('ConversationNode')  
        #      .has('creatorId', creator_id)  
        #      .has('date', between(start_dt, end_dt))  
        #      .values('sentimentScore')...  
        points = [  
            SentimentTrendPoint(date=datetime(2025, 1, 1), sentiment_score=0.75),  
            SentimentTrendPoint(date=datetime(2025, 1, 2), sentiment_score=0.8)  
        ]  
        result = SentimentTrendResponse(trend=points)  
        logger.debug(f"[INSIGHTS] Sentiment trend points: {len(points)}")  
        return result  
    except Exception as e:  
        logger.exception(f"[INSIGHTS] fetch_sentiment_trend failed: {e}")  
        return SentimentTrendResponse(trend=[])  
  
  
async def fetch_response_time_metrics(  
    start_date: Optional[datetime],  
    end_date: Optional[datetime],  
    creator_id: Optional[str] = None  
) -> ResponseTimeMetricsResponse:  
    """Compute average handling time, silence percentage, and turns."""  
    start_date, end_date = _default_date_range(start_date, end_date)  
    start_dt = normalize_datetime(start_date)  
    end_dt = normalize_datetime(end_date)  
    creator_id = _normalize_creator_id(creator_id)  
  
    logger.info(f"[INSIGHTS] Fetching response time metrics for creator={creator_id or 'ALL'} from {start_dt} to {end_dt}")  
  
    try:  
        # TODO: Replace with actual Gremlin query filtered by creator_id:  
        # g.V().hasLabel('ConversationNode')  
        #      .has('creatorId', creator_id)  
        #      .has('date', between(start_dt, end_dt))  
        #      .values('averageHandlingTime')...  
        result = ResponseTimeMetricsResponse(  
            average_handling_time_minutes=15.2,  
            silence_percentage=35.0,  
            turns=5.4  
        )  
        logger.debug(f"[INSIGHTS] Response time metrics: {result.model_dump()}")  
        return result  
    except Exception as e:  
        logger.exception(f"[INSIGHTS] fetch_response_time_metrics failed: {e}")  
        return ResponseTimeMetricsResponse(  
            average_handling_time_minutes=0.0,  
            silence_percentage=0.0,  
            turns=0.0  
        )  
  
# ----------------------------  
# Build full analytics payload  
# ----------------------------  
  
async def build_analytics_update(  
    user_id: str,  
    start_date: Optional[datetime],  
    end_date: Optional[datetime],  
    creator_id: Optional[str] = None  
) -> AnalyticsUpdate:  
    """  
    Fetch all analytics metrics and construct a full AnalyticsUpdate payload  
    including per-conversation priority scores and unread counts.  
    """  
    start_date, end_date = _default_date_range(start_date, end_date)  
    creator_id = _normalize_creator_id(creator_id)  
  
    try:  
        topics = await fetch_topic_metrics(start_date, end_date, creator_id)  
    except Exception as e:  
        logger.exception(f"[INSIGHTS] Topic metrics fetch failed for user={user_id}, creator={creator_id}: {e}")  
        topics = []  
  
    try:  
        sentiment_trend = await fetch_sentiment_trend(start_date, end_date, creator_id)  
    except Exception as e:  
        logger.exception(f"[INSIGHTS] Sentiment trend fetch failed for user={user_id}, creator={creator_id}: {e}")  
        sentiment_trend = SentimentTrendResponse(trend=[])  
  
    try:  
        response_time_metrics = await fetch_response_time_metrics(start_date, end_date, creator_id)  
    except Exception as e:  
        logger.exception(f"[INSIGHTS] Response time metrics fetch failed for user={user_id}, creator={creator_id}: {e}")  
        response_time_metrics = ResponseTimeMetricsResponse(  
            average_handling_time_minutes=0.0,  
            silence_percentage=0.0,  
            turns=0.0  
        )  
  
    # TODO: Replace with actual Gremlin traversal logic to compute per-conversation values  
    priority_scores: Dict[str, float] = {  
        "conv1": 95.0,  
        "conv2": 78.5  
    }  
    unread_counts: Dict[str, int] = {  
        "conv1": 2,  
        "conv2": 0  
    }  
  
    payload = AnalyticsUpdate(  
        topics=topics,  
        sentiment_trend=sentiment_trend,  
        response_time_metrics=response_time_metrics,  
        priorityScores=priority_scores,  
        unreadCounts=unread_counts  
    )  
    return payload  
  
# ----------------------------  
# Broadcaster hook  
# ----------------------------  
  
async def broadcast_analytics_update(  
    user_id: str,  
    start_date: Optional[datetime],  
    end_date: Optional[datetime],  
    creator_id: Optional[str] = None  
) -> None:  
    """Fetch analytics metrics and broadcast them to the frontend via Redis Pub/Sub."""  
    creator_id = _normalize_creator_id(creator_id)  
    logger.info(f"[INSIGHTS] Broadcasting analytics update for user={user_id}, creator={creator_id or 'ALL'}")  
  
    try:  
        payload = await build_analytics_update(user_id, start_date, end_date, creator_id)  
        msg = AnalyticsUpdateMsg(  
            type="analytics_update",  
            payload=payload  
        )  
        await broadcast.publish(  
            channel=f"frontend_user_{user_id}",  
            message=msg.model_dump_json()  
        )  
        logger.debug(f"[INSIGHTS] Analytics update broadcast for user={user_id}, creator={creator_id}")  
    except Exception as e:  
        logger.exception(f"[INSIGHTS] Failed to broadcast analytics update for user={user_id}, creator={creator_id}: {e}")  
        await broadcast_system_error(user_id, "analytics_broadcast_failed", str(e))  
  
# ----------------------------  
# Full snapshot services  
# ----------------------------  
  
async def fetch_conversations_for_user(  
    user_id: str,  
    creator_id: Optional[str] = None  
) -> List[ExtendedConversationNode]:  
    """  
    Fetch all conversations for a given user from the graph DB.  
    TODO: Replace with actual Gremlin query to Cosmos DB.  
    """  
    creator_id = _normalize_creator_id(creator_id)  
    logger.info(f"[SNAPSHOT] Fetching conversations for user={user_id}, creator={creator_id or 'ALL'}")  
    # TODO: g.V().hasLabel('ConversationNode')  
    #          .has('creatorId', creator_id)  
    #          .has('userId', user_id)...  
    return []  # Stub until Gremlin query implemented  
  
  
async def get_full_snapshot(  
    user_id: str,  
    creator_id: Optional[str] = None  
) -> FullSyncResponse:  
    """  
    Returns the latest snapshot of all conversations + analytics for a given user_id.  
    Matches the WS 'full_sync_response' payload.  
    """  
    creator_id = _normalize_creator_id(creator_id)  
    logger.info(f"[SNAPSHOT] Building full snapshot for user={user_id}, creator={creator_id or 'ALL'}")  
  
    try:  
        conversations = await fetch_conversations_for_user(user_id, creator_id)  
        logger.debug(f"[SNAPSHOT] Retrieved {len(conversations)} conversations for user={user_id}")  
    except Exception as e:  
        logger.exception(f"[SNAPSHOT] Failed to fetch conversations for user={user_id}, creator={creator_id}: {e}")  
        conversations = []  
  
    try:  
        analytics = await build_analytics_update(user_id, None, None, creator_id)  
    except Exception as e:  
        logger.exception(f"[SNAPSHOT] Failed to build analytics for user={user_id}, creator={creator_id}: {e}")  
        analytics = AnalyticsUpdate(  
            topics=[],  
            sentiment_trend=SentimentTrendResponse(trend=[]),  
            response_time_metrics=ResponseTimeMetricsResponse(  
                average_handling_time_minutes=0.0,  
                silence_percentage=0.0,  
                turns=0.0  
            ),  
            priorityScores={},  
            unreadCounts={}  
        )  
  
    return FullSyncResponse(conversations=conversations, analytics=analytics)  