import { AnalyticsPresentation } from '../components/analytics';
import { analyticsStoreActions, useAnalyticsStore } from '../store/analyticsStore';

export default function AnalyticsView() {
  const state = useAnalyticsStore((store) => store.state);
  const dateRange = useAnalyticsStore((store) => store.dateRange);

  return (
    <AnalyticsPresentation
      state={state}
      dateRange={dateRange}
      onDateRangeChange={(range) => void analyticsStoreActions.setDateRange(range)}
    />
  );
}
