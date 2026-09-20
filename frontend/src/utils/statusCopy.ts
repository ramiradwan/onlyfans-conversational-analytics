import type {
  AgentStatePayload,
  HistoricalCoverage,
  LiveFreshness,
  ProtocolErrorPayload,
  ProjectionState,
} from '../protocol';
import { isConfigurationAligned } from './dataReadiness';

/**
 * Plain-language status copy for creators. Views derive every status sentence from
 * structured state here; diagnostic strings from Brain are never rendered.
 */

export type AgentSummary = Pick<
  AgentStatePayload,
  | 'applied_config_revision'
  | 'applied_history_settings_revision'
  | 'degraded_reason'
  | 'required_config_revision'
  | 'required_history_settings_revision'
  | 'status'
>;

export type ExtensionConnection =
  | 'connected'
  | 'offline'
  | 'not_responding'
  | 'applying_settings'
  | 'settings_problem';

export function extensionConnection(agent: AgentSummary | null): ExtensionConnection {
  if (agent === null || agent.status === 'disconnected') return 'offline';
  if (agent.status === 'stale') return 'not_responding';
  if (!isConfigurationAligned(agent)) return 'applying_settings';
  return agent.degraded_reason ? 'settings_problem' : 'connected';
}

export interface StatusMessage {
  detail: string;
  severity: 'error' | 'warning' | 'info';
  title: string;
}

const EXTENSION_ISSUES: Record<Exclude<ExtensionConnection, 'connected'>, StatusMessage> = {
  offline: {
    title: 'Connection interrupted',
    detail: "New messages won't arrive until the browser extension reconnects. Check that your browser is open and the extension is on.",
    severity: 'warning',
  },
  not_responding: {
    title: 'Browser extension not responding',
    detail: 'New messages may be delayed. Check that your browser is open.',
    severity: 'warning',
  },
  applying_settings: {
    title: 'Applying your settings',
    detail: 'Waiting for the browser extension to apply your latest settings.',
    severity: 'info',
  },
  settings_problem: {
    title: 'Browser extension needs attention',
    detail: "It couldn't apply your latest settings, so new messages may be delayed.",
    severity: 'warning',
  },
};

export function extensionIssue(connection: ExtensionConnection): StatusMessage | null {
  return connection === 'connected' ? null : EXTENSION_ISSUES[connection];
}

export function extensionLabel(connection: ExtensionConnection): string {
  switch (connection) {
    case 'connected':
      return 'Connected';
    case 'offline':
      return 'Connection interrupted';
    case 'not_responding':
      return 'Not responding';
    case 'applying_settings':
      return 'Applying settings';
    case 'settings_problem':
      return 'Needs attention';
  }
}

export function protocolErrorText(error: Pick<ProtocolErrorPayload, 'code' | 'fatal'>): string {
  if (error.code === 'unauthorized') return 'Your session ended. Reload the page to sign in again.';
  if (error.code === 'unsupported_version') return 'This page is out of date. Reload the page.';
  return error.fatal
    ? 'Something went wrong. Reload the page to try again.'
    : 'Something went wrong with the last update. If this keeps happening, reload the page.';
}

/** True until the creator has started message history for the first time. */
export function setupIncomplete(coverage: Pick<HistoricalCoverage, 'phase' | 'status'>): boolean {
  return coverage.status !== 'complete' && coverage.phase === 'not_started';
}

export function insightsLabel(projection: ProjectionState): string {
  if (projection.status === 'unavailable' || projection.status === 'degraded') return 'Not available';
  if (projection.status === 'pending' || projection.projected_revision < projection.canonical_revision) {
    return 'Updating';
  }
  return 'Up to date';
}

export function newMessagesLabel(freshness: Pick<LiveFreshness, 'status'>): string {
  if (freshness.status === 'current') return 'Up to date';
  return freshness.status === 'delayed' ? 'Delayed' : 'Waiting';
}
