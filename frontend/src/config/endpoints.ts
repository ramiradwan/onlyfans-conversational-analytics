// ⚠️ AUTO-GENERATED FILE — DO NOT EDIT  
// Generated from http://localhost:8000/openapi.json on 2025-11-16T16:34:01.691Z  
  
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';  
export const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || 'ws://localhost:8000';  
  
// --- REST API Endpoints ---  
export const GET_TOPICS = `/api/insights/api/v1/insights/topics`;
export const GET_SENTIMENT_TREND = `/api/insights/api/v1/insights/sentiment-trend`;
export const GET_RESPONSE_TIME_METRICS = `/api/insights/api/v1/insights/response-time`;
export const GET_FULL_ANALYTICS = `/api/insights/api/v1/insights/full`;
export const GET_WSS_SCHEMA = `/api/v1/schemas/wss`;
export const SERVE_FRONTEND = `/`;
export const BOOTSTRAP_FRONTEND_STATE = `/api/v1/frontend/bootstrap/{user_id}`;
export const HEALTH_CHECK_HEALTH_GET = `/health`;  
  
// --- WebSocket Endpoints ---  
export const WS_EXTENSION = (userId: string) => `/ws/extension/${userId}`;
export const WS_FRONTEND = (userId: string) => `/ws/frontend/${userId}`;
export const WS_CHATWOOT = (userId: string) => `/ws/chatwoot/${userId}`;  
