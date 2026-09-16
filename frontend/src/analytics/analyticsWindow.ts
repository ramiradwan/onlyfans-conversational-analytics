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
    const start = formatter.format(new Date(window.start));
    const end = formatter.format(new Date(window.end));
    return start === end ? `Messages from ${start}` : `Messages from ${start} to ${end}`;
  }
  if (source.window.scope === 'requested') return 'No messages in these dates';
  return 'No messages yet';
}
