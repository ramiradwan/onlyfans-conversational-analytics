"""  
Insights routes for OnlyFans Conversational Analytics.  
  
Provides:  
- Topic metrics over a date range.  
- Sentiment trend over a date range.  
- Response time metrics over a date range.  
"""  
  
from fastapi import APIRouter, Query, HTTPException  
from typing import Optional, List  
from datetime import datetime  
  
from app.models.insights import (  
    TopicMetricsResponse,  
    SentimentTrendResponse,  
    ResponseTimeMetricsResponse  
)  
from app.services import insights_service  
from app.utils.logger import logger  
  
router = APIRouter(prefix="/api/v1/insights", tags=["Insights"])  
  
  
@router.get("/topics", response_model=List[TopicMetricsResponse])  
async def get_topic_metrics(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None),  
    broadcast: bool = Query(False, description="If true, broadcast analytics update via WS")  
):  
    """  
    Get volume, percentage of total, and trend for each topic over the given date range.  
    Optionally broadcast the update to the frontend.  
    """  
    try:  
        logger.info(f"[ROUTE] get_topic_metrics start_date={start_date}, end_date={end_date}")  
        metrics = await insights_service.fetch_topic_metrics(start_date, end_date)  
        logger.debug(f"[ROUTE] get_topic_metrics returned {len(metrics)} topics")  
  
        if broadcast:  
            # Assuming user_id is known from auth context  
            # Here you'd inject it from request/session  
            user_id = "demo_user"  
            await insights_service.broadcast_analytics_update(user_id, start_date, end_date)  
  
        return metrics  
    except Exception as e:  
        logger.exception("Error fetching topic metrics")  
        raise HTTPException(status_code=500, detail={"code": "insights_error", "message": str(e)})  
  
  
@router.get("/sentiment-trend", response_model=SentimentTrendResponse)  
async def get_sentiment_trend(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None)  
):  
    """  
    Get average sentiment score trend over time for conversations in the given date range.  
    """  
    try:  
        logger.info(f"[ROUTE] get_sentiment_trend start_date={start_date}, end_date={end_date}")  
        trend = await insights_service.fetch_sentiment_trend(start_date, end_date)  
        logger.debug(f"[ROUTE] get_sentiment_trend returned {len(trend.trend)} points")  
        return trend  
    except Exception as e:  
        logger.exception("Error fetching sentiment trend")  
        raise HTTPException(status_code=500, detail={"code": "insights_error", "message": str(e)})  
  
  
@router.get("/response-time", response_model=ResponseTimeMetricsResponse)  
async def get_response_time_metrics(  
    start_date: Optional[datetime] = Query(None),  
    end_date: Optional[datetime] = Query(None)  
):  
    """  
    Get average handling time (AHT), silence percentage, and turns metrics over the given date range.  
    """  
    try:  
        logger.info(f"[ROUTE] get_response_time_metrics start_date={start_date}, end_date={end_date}")  
        metrics = await insights_service.fetch_response_time_metrics(start_date, end_date)  
        logger.debug(f"[ROUTE] get_response_time_metrics returned {metrics.model_dump()}")  
        return metrics  
    except Exception as e:  
        logger.exception("Error fetching response time metrics")  
        raise HTTPException(status_code=500, detail={"code": "insights_error", "message": str(e)})  