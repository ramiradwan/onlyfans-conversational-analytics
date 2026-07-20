import type {
  AnalyticsWindowDocument,
  SliceProvenanceDocument,
} from './analyticsContract';

export interface AnalyticsWindowSource {
  window: AnalyticsWindowDocument;
  provenance: SliceProvenanceDocument;
}

export interface AnalyticsWindowSources {
  creatorMetrics: AnalyticsWindowSource;
  responseMetrics: AnalyticsWindowSource;
  sentimentTrend: AnalyticsWindowSource;
  topics: AnalyticsWindowSource;
  conversationInsights: AnalyticsWindowSource;
  graph: AnalyticsWindowSource;
}

export function analyticsWindowLabel(source: AnalyticsWindowSource): string {
  const window = source.provenance.effective_window;
  if (window.start && window.end) {
    const formatter = new Intl.DateTimeFormat(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      timeZone: 'UTC',
    });
    return `${formatter.format(new Date(window.start))} – ${formatter.format(new Date(window.end))} UTC`;
  }
  if (source.window.scope === 'all_time') return 'All-time';
  if (source.window.scope === 'requested') return 'Requested range · no eligible samples';
  return 'No eligible samples';
}
