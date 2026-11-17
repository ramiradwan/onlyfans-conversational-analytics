import type {
  AgentStatePayload,
  AnalyticsMetric,
  HistoricalCoverage,
  LiveFreshness,
  ProjectionState,
} from '../protocol';

export interface DataReadiness {
  coverage: HistoricalCoverage;
  projection: ProjectionState;
  liveFreshness: LiveFreshness;
  configurationAligned: boolean;
}

type ConfigurationRevisions = Pick<
  AgentStatePayload,
  | 'applied_config_revision'
  | 'applied_history_settings_revision'
  | 'required_config_revision'
  | 'required_history_settings_revision'
>;

export function isConfigurationAligned(agent: ConfigurationRevisions | null): boolean {
  return (
    agent !== null &&
    agent.applied_config_revision !== null &&
    agent.required_config_revision === agent.applied_config_revision &&
    agent.applied_history_settings_revision !== null &&
    agent.required_history_settings_revision === agent.applied_history_settings_revision
  );
}

export function isFullyCurrent(readiness: DataReadiness): boolean {
  return (
    readiness.coverage.status === 'complete' &&
    readiness.projection.status === 'current' &&
    readiness.projection.projected_revision >= readiness.projection.canonical_revision &&
    readiness.liveFreshness.status === 'current' &&
    readiness.configurationAligned
  );
}

/**
 * Additive metrics remain useful during backfill, but must be labelled as lower bounds.
 * An unproven zero is unknown, never an exact zero.
 */
export function formatAdditiveMetric(
  metric: AnalyticsMetric | null | undefined,
  readiness: DataReadiness,
  formatter = new Intl.NumberFormat().format,
): string {
  if (
    metric?.value === null ||
    metric?.value === undefined ||
    readiness.projection.status !== 'current' ||
    metric.projection_revision !== readiness.projection.projected_revision
  ) {
    return '—';
  }
  if (metric.basis === 'synced_subset' && metric.value === 0) return 'None yet';
  return `${formatter(metric.value)}${metric.basis === 'synced_subset' ? '+' : ''}`;
}

export interface MetricEvidence {
  asOf: string;
  observedEnd: string | null;
  observedStart: string | null;
  partial: boolean;
  revision: number;
}

/** Combines metric envelopes into one conservative basis: partial if any is partial, oldest as-of, lowest revision. */
export function summarizeMetricEvidence(
  metrics: readonly (AnalyticsMetric | null | undefined)[],
): MetricEvidence | null {
  const present = metrics.filter((metric): metric is AnalyticsMetric => Boolean(metric));
  if (present.length === 0) return null;
  const oldest = present.reduce((current, metric) =>
    Date.parse(metric.as_of) < Date.parse(current.as_of) ? metric : current,
  );
  return {
    asOf: oldest.as_of,
    observedEnd: oldest.observed_range.end,
    observedStart: oldest.observed_range.start,
    partial: present.some((metric) => metric.basis !== 'complete'),
    revision: Math.min(...present.map((metric) => metric.projection_revision)),
  };
}

const COVERAGE_REASON_TEXT: Record<string, string> = {
  consent_revoked: 'Message history sync was turned off.',
  configuration_not_applied: "The browser extension hasn't applied your latest settings yet.",
  history_sync_paused: 'Message history sync is paused.',
};

export function humanizeProjectionReason(
  reason: string | null,
  whenAbsent: string,
  overrides?: Record<string, string>,
): string {
  if (reason === null) return whenAbsent;
  // Projection reasons do not establish active work or a customer remedy.
  // The caller's state title carries availability; only explicit copy adds detail.
  return overrides?.[reason] ?? '';
}

export function humanizeCoverageReason(reason: string | null, whenAbsent: string): string {
  if (reason === null) return whenAbsent;
  return COVERAGE_REASON_TEXT[reason] ?? whenAbsent;
}

/** The inventory is frozen only when the owner enters backfill. */
export function coverageProgressPercent(coverage: HistoricalCoverage): number | null {
  if (coverage.status === 'complete') return 100;
  const total = coverage.discovered_conversations;
  if (coverage.phase !== 'backfilling' || total === null || total <= 0
    || coverage.complete_conversations >= total) return null;
  return Math.min(99, Math.round(coverage.complete_conversations / total * 100));
}

export function coverageProgressLabel(coverage: HistoricalCoverage): string {
  if (coverage.status === 'complete') return coverage.discovered_conversations === null ? 'History saved.' : `History saved for ${new Intl.NumberFormat('en-US').format(coverage.complete_conversations)} chats`;
  if (coverage.phase === 'paused') return 'History paused';
  if (coverage.phase === 'blocked') return 'History needs attention';
  if (coverage.phase === 'not_started') return 'History not started';
  const percent = coverageProgressPercent(coverage);
  if (percent !== null) return `History ${percent}% synced`;
  if (coverage.phase === 'discovering') return 'Finding conversations';
  if (coverage.phase === 'backfilling' && coverage.discovered_conversations !== null
    && coverage.complete_conversations >= coverage.discovered_conversations) return 'Checking saved history';
  return coverage.phase === 'repairing' ? 'Checking message history' : 'Syncing history';
}
