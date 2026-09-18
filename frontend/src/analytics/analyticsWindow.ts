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

function dateFormat(): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat(undefined, {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

/** Localized range that shares month and year where it can, such as "Jun 1 – 30, 2026". */
function formatRange(first: Date, second: Date): string {
  const [start, end] = first <= second ? [first, second] : [second, first];
  return dateFormat().formatRange(start, end);
}

/** Parses a `YYYY-MM-DD` calendar date as UTC midnight. */
function calendarDate(value: string): Date {
  return new Date(`${value}T00:00:00Z`);
}

/** Localized calendar date, such as "Jun 1, 2026". */
export function formatCalendarDate(value: string): string {
  return dateFormat().format(calendarDate(value));
}

export function formatCalendarRange(start: string, end: string): string {
  return formatRange(calendarDate(start), calendarDate(end));
}

export function analyticsWindowLabel(source: AnalyticsWindowSource): string {
  const window = source.provenance.effective_window;
  if (window.start && window.end) {
    return `Messages available: ${formatRange(new Date(window.start), new Date(window.end))}`;
  }
  if (source.window.scope === 'requested') return 'No messages in these dates';
  return 'No messages yet';
}
