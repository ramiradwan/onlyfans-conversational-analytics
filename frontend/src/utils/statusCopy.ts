import type {
  AgentStatePayload,
  HistoricalCoverage,
  LiveFreshness,
  ProtocolErrorPayload,
  ProjectionState,
} from '../protocol';
import { isConfigurationAligned } from './dataReadiness';
import { protocolRecovery, recoveryClass } from '../../shared-setup/recovery.js';

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
    detail: 'New messages cannot reach this app while the extension is disconnected.',
    severity: 'warning',
  },
  not_responding: {
    title: 'Browser extension not responding',
    detail: 'The app has not received a recent extension response.',
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
      return 'Not connected';
    case 'not_responding':
      return 'Not responding';
    case 'applying_settings':
      return 'Applying settings';
    case 'settings_problem':
      return 'Needs attention';
  }
}

export function protocolErrorText(error: Pick<ProtocolErrorPayload, 'code' | 'fatal'>): string {
  const classification = recoveryClass(protocolRecovery, error.code);
  if (classification === 'authentication_required') return 'Your session ended. Reload the page to sign in again.';
  if (classification === 'version_incompatible') return 'This page is out of date. Reload the page.';
  return error.fatal
    ? "Dipsy couldn't continue the connection."
    : 'The last update could not be verified.';
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
  return 'Insights updated';
}

export function newMessagesLabel(freshness: Pick<LiveFreshness, 'status'>): string {
  if (freshness.status === 'current') return 'Up to date';
  return freshness.status === 'delayed' ? 'Delayed' : 'Waiting';
}
