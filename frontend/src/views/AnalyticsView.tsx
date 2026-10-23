import { useMemo } from 'react';
import { findingsBaseline } from '../analytics';
import { AnalyticsPresentation } from '../components/analytics';
import { ConversationQuestions } from '../components/analytics/ConversationQuestions';
import { TaskFindings } from '../components/analytics/TaskFindings';
import { analyticsStoreActions, useAnalyticsStore } from '../store/analyticsStore';

export default function AnalyticsView() {
  const state = useAnalyticsStore((store) => store.state);
  const dateRange = useAnalyticsStore((store) => store.dateRange);
  const baseline = useMemo(() => (state.status === 'baseline' || state.status === 'model') && !state.isRefreshing
    ? findingsBaseline(state.data, dateRange) : null, [state, dateRange]);

  return (
    <AnalyticsPresentation
      questions={<ConversationQuestions />}
      findings={<TaskFindings baseline={baseline} />}
      state={state}
      dateRange={dateRange}
      onDateRangeChange={(range) => void analyticsStoreActions.setDateRange(range)}
      onRetry={() => void analyticsStoreActions.refresh()}
    />
  );
}
