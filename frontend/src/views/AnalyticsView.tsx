import { useMemo, useSyncExternalStore } from 'react';
import { findingsBaseline } from '../analytics';
import { AnalyticsPresentation } from '../components/analytics';
import { ConversationQuestions } from '../components/analytics/ConversationQuestions';
import { TaskFindings } from '../components/analytics/TaskFindings';
import { TextAnalysisPreviews } from '../components/analytics/TextAnalysisPreviews';
import { analyticsStoreActions, useAnalyticsStore } from '../store/analyticsStore';
import { bridgeTransportStore } from '../store/transportStore';

export default function AnalyticsView() {
  const account = useSyncExternalStore(bridgeTransportStore.subscribe, () => bridgeTransportStore.getState().creatorAccountId);
  const state = useAnalyticsStore((store) => store.state);
  const dateRange = useAnalyticsStore((store) => store.dateRange);
  const baseline = useMemo(() => (state.status === 'baseline' || state.status === 'model') && !state.isRefreshing
    ? findingsBaseline(state.data, dateRange) : null, [state, dateRange]);

  return (
    <AnalyticsPresentation
      questions={<ConversationQuestions />}
      findings={<><TaskFindings baseline={baseline} onRefreshBaseline={analyticsStoreActions.refresh} /><TextAnalysisPreviews creatorAccountId={account} /></>}
      state={state}
      dateRange={dateRange}
      onDateRangeChange={(range) => void analyticsStoreActions.setDateRange(range)}
      onRetry={() => void analyticsStoreActions.refresh()}
    />
  );
}
