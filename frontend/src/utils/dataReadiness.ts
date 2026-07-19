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

export function isConfigurationAligned(agent: AgentStatePayload | null): boolean {
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

export function canShowCompleteAnalytics(readiness: DataReadiness): boolean {
  return (
    readiness.coverage.status === 'complete' &&
    readiness.projection.status === 'current' &&
    readiness.projection.projected_revision >= readiness.projection.canonical_revision
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
    readiness.projection.status !== 'current'
  ) {
    return '—';
  }
  if (metric.basis === 'synced_subset' && metric.value === 0) return '0 in synced messages';
  return `${formatter(metric.value)}${metric.basis === 'synced_subset' ? '+' : ''}`;
}

export function metricEvidenceLabel(metric: AnalyticsMetric | null | undefined): string | undefined {
  if (!metric) return undefined;
  const basis = metric.basis === 'complete' ? 'Complete range' : 'Based on synced messages';
  return `${basis} · sample ${new Intl.NumberFormat().format(metric.sample_size)} · As of ${new Date(metric.as_of).toLocaleString()}`;
}

export function coverageProgressLabel(coverage: HistoricalCoverage): string {
  if (coverage.status === 'complete') return 'Historical coverage complete';
  if (coverage.phase === 'paused') return 'Historical sync paused';
  if (coverage.phase === 'blocked') return 'Historical sync needs attention';
  if (coverage.phase === 'not_started') return 'Historical sync not started';
  if (coverage.discovered_conversations && coverage.discovered_conversations > 0) {
    const percent = Math.min(
      100,
      Math.round(
        (coverage.complete_conversations / coverage.discovered_conversations) * 100,
      ),
    );
    return `Historical coverage ${percent}%`;
  }
  return coverage.phase === 'discovering'
    ? 'Discovering conversations'
    : 'Building historical coverage';
}
