import { create } from 'zustand';  
import {  
  AnalyticsUpdate,  
  TopicMetricsResponse,  
  SentimentTrendPoint,  
  ResponseTimeMetricsResponse,  
} from '@/types/backend-wss';  
  
export interface AnalyticsStoreState {  
  isLoaded: boolean;  
  topics: TopicMetricsResponse[];  
  sentimentTrend: SentimentTrendPoint[];  
  responseTimeMetrics: ResponseTimeMetricsResponse | null;  
  priorityScores: Record<string, number>;  
  unreadCounts: Record<string, number>;  
  actions: {  
    handleAnalyticsUpdate: (payload: AnalyticsUpdate) => void;  
  };  
}  
  
export const useAnalyticsStore = create<AnalyticsStoreState>((set, get) => ({  
  isLoaded: false,  
  topics: [],  
  sentimentTrend: [],  
  responseTimeMetrics: null,  
  priorityScores: {},  
  unreadCounts: {},  
  actions: {  
    handleAnalyticsUpdate: (payload) => {  
      const current = get();  
  
      const nextTopics = payload.topics || [];  
      const nextTrend = payload.sentiment_trend?.trend || [];  
      const nextResponseTime = payload.response_time_metrics || null;  
      const nextPriorityScores = payload.priorityScores || {};  
      const nextUnreadCounts = payload.unreadCounts || {};  
  
      // ✅ Use reference equality to avoid unnecessary updates  
      if (  
        current.topics !== nextTopics ||  
        current.sentimentTrend !== nextTrend ||  
        current.responseTimeMetrics !== nextResponseTime ||  
        current.priorityScores !== nextPriorityScores ||  
        current.unreadCounts !== nextUnreadCounts ||  
        current.isLoaded !== true  
      ) {  
        set({  
          topics: nextTopics,  
          sentimentTrend: nextTrend,  
          responseTimeMetrics: nextResponseTime,  
          priorityScores: nextPriorityScores,  
          unreadCounts: nextUnreadCounts,  
          isLoaded: true,  
        });  
      }  
    },  
  },  
}));  
  
export const analyticsStoreActions = useAnalyticsStore.getState().actions;  