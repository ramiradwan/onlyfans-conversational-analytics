"""  
Insights routes for OnlyFans Conversational Analytics.  
  
Provides:  
    - Topic metrics over a date range.  
    - Sentiment trend over a date range.  
    - Response time metrics over a date range.  
    - (NEW) Full analytics update payload, including priorityScores and unreadCounts.  
"""  
  
from fastapi import APIRouter, Query, HTTPException, Depends  
from typing import Optional, List  
from datetime import datetime  
  
from app.models.insights import (  
    TopicMetricsResponse,  
    SentimentTrendResponse,  
    ResponseTimeMetricsResponse,  
    AnalyticsUpdate,  
)  
from app.services import insights_service  
from app.utils.logger import logger  
  
router = APIRouter(prefix="/api/v1/insights", tags=["Insights"])  
  
# Placeholder auth dependency — replace with actual  
def get_current_user_id() -> str:  
    return "demo_user"  
  
@router.get("/topics", response_model=List[TopicMetricsResponse])  
async def get_topic_metrics(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None),  
    creator_id: Optional[str] = Query(None, description="Filter metrics for a specific creator"),  
    broadcast: bool = Query(False, description="If true, broadcast analytics update via WS"),  
    user_id: str = Depends(get_current_user_id)  
):  
    """Get volume, percentage of total, and trend for each topic over the given date range."""  
    try:  
        logger.info(f"[ROUTE] get_topic_metrics user={user_id} creator={creator_id} start={start_date} end={end_date}")  
        metrics = await insights_service.fetch_topic_metrics(start_date, end_date, creator_id=creator_id)  
        logger.debug(f"[ROUTE] get_topic_metrics returned {len(metrics)} topics")  
  
        if broadcast:  
            await insights_service.broadcast_analytics_update(user_id, start_date, end_date, creator_id=creator_id)  
  
        return metrics  
    except Exception as e:  
        logger.exception("Error fetching topic metrics")  
        raise HTTPException(status_code=500, detail={"code": "insights_error", "message": str(e)})  
  
  
@router.get("/sentiment-trend", response_model=SentimentTrendResponse)  
async def get_sentiment_trend(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None),  
    creator_id: Optional[str] = Query(None),  
    user_id: str = Depends(get_current_user_id)  
):  
    """Get average sentiment score trend over time for conversations in the given date range."""  
    try:  
        logger.info(f"[ROUTE] get_sentiment_trend user={user_id} creator={creator_id} start={start_date} end={end_date}")  
        trend = await insights_service.fetch_sentiment_trend(start_date, end_date, creator_id=creator_id)  
        logger.debug(f"[ROUTE] get_sentiment_trend returned {len(trend.trend)} points")  
        return trend  
    except Exception as e:  
        logger.exception("Error fetching sentiment trend")  
        raise HTTPException(status_code=500, detail={"code": "insights_error", "message": str(e)})  
  
  
@router.get("/response-time", response_model=ResponseTimeMetricsResponse)  
async def get_response_time_metrics(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None),  
    creator_id: Optional[str] = Query(None),  
    user_id: str = Depends(get_current_user_id)  
):  
    """Get average handling time (AHT), silence percentage, and turns metrics over the given date range."""  
    try:  
        logger.info(f"[ROUTE] get_response_time_metrics user={user_id} creator={creator_id} start={start_date} end={end_date}")  
        metrics = await insights_service.fetch_response_time_metrics(start_date, end_date, creator_id=creator_id)  
        logger.debug(f"[ROUTE] get_response_time_metrics returned {metrics.model_dump()}")  
        return metrics  
    except Exception as e:  
        logger.exception("Error fetching response time metrics")  
        raise HTTPException(status_code=500, detail={"code": "insights_error", "message": str(e)})  
  
  
@router.get("/full", response_model=AnalyticsUpdate)  
async def get_full_analytics(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None),  
    creator_id: Optional[str] = Query(None),  
    broadcast: bool = Query(False, description="If true, also broadcast via WS"),  
    user_id: str = Depends(get_current_user_id)  
):  
    """  
    Get the complete analytics update payload, including topics, sentiment trend,  
    response time metrics, and per-conversation priorityScores/unreadCounts.  
    """  
    try:  
        logger.info(f"[ROUTE] get_full_analytics user={user_id} creator={creator_id} start={start_date} end={end_date}")  
        payload = await insights_service.build_analytics_update(user_id, start_date, end_date, creator_id=creator_id)  
  
        if broadcast:  
            await insights_service.broadcast_analytics_update(user_id, start_date, end_date, creator_id=creator_id)  
  
        return payload  
    except Exception as e:  
        logger.exception("Error fetching full analytics payload")  
        raise HTTPException(status_code=500, detail={"code": "insights_error", "message": str(e)})  