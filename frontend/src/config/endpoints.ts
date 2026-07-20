export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://bridge.localhost:17871';
export const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || 'ws://bridge.localhost:17871';

export const SERVE_FRONTEND = '/';
export const BOOTSTRAP_FRONTEND_STATE = '/api/v1/frontend/bootstrap';
export const GET_HISTORY_SETTINGS = '/api/v1/settings/history';
export const PUT_HISTORY_SETTINGS = '/api/v1/settings/history';
export const DELETE_HISTORY_CONSENT = '/api/v1/settings/history/consent';
export const conversationMessagesPath = (conversationId: string) =>
  `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`;
export const HEALTH_CHECK = '/health';

export const WS_AGENT = '/ws/agent';
export const WS_BRIDGE = '/ws/bridge';

export const INSIGHTS_FULL = '/api/v1/insights/full';
export const INSIGHTS_PROJECTION = '/api/v1/insights/projection';
export const INSIGHTS_TOPICS = '/api/v1/insights/topics';
export const INSIGHTS_SENTIMENT_TREND = '/api/v1/insights/sentiment-trend';
export const INSIGHTS_RESPONSE_TIME = '/api/v1/insights/response-time';
