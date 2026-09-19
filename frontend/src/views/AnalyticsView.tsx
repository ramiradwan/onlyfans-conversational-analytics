import { AnalyticsPresentation } from '../components/analytics';
import { ConversationQuestions } from '../components/analytics/ConversationQuestions';
import { analyticsStoreActions, useAnalyticsStore } from '../store/analyticsStore';

export default function AnalyticsView() {
  const state = useAnalyticsStore((store) => store.state);
  const dateRange = useAnalyticsStore((store) => store.dateRange);

  return (
    <AnalyticsPresentation
      questions={<ConversationQuestions />}
      state={state}
      dateRange={dateRange}
      onDateRangeChange={(range) => void analyticsStoreActions.setDateRange(range)}
      onRetry={() => void analyticsStoreActions.refresh()}
    />
  );
}
