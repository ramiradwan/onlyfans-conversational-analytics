import type { AgentStatePayload, HistoricalCoverage, HistorySettings } from '../protocol';

export interface HistoryPresentation {
  label: string;
  detail: string;
  tone: 'default' | 'warning' | 'info' | 'success';
}

/** Consent, requested state and acquisition evidence remain separate owner facts. */
export function presentHistory(settings: HistorySettings, agent: AgentStatePayload | null,
  coverage: HistoricalCoverage): HistoryPresentation {
  if (!settings.consent_revision || settings.desired_state === 'revoked') return {
    label: 'Off', detail: 'Add older conversations to the messages saved on this computer.', tone: 'default',
  };
  if (settings.desired_state === 'paused') return settings.effective_state === 'paused'
    ? { label: 'Paused', detail: 'Older messages are not being added.', tone: 'default' }
    : { label: 'Pausing', detail: 'Waiting for the extension to confirm the pause.', tone: 'info' };
  if (coverage.status === 'complete') return {
    label: 'Saved', detail: coverage.discovered_conversations === null ? 'Available history is saved.'
      : `History saved for ${new Intl.NumberFormat().format(coverage.complete_conversations)} chats.`, tone: 'success',
  };
  if (!agent || agent.status !== 'connected') return {
    label: 'Not connected', detail: 'History collection is waiting for the browser extension to connect.', tone: 'warning',
  };
  if (agent.browser?.history_permission === 'missing') return {
    label: 'Permission needed', detail: 'Allow Chrome to read older messages for this app.', tone: 'warning',
  };
  if (settings.desired_state !== settings.effective_state) return {
    label: 'Starting', detail: 'Waiting for the extension to apply message history settings.', tone: 'info',
  };
  if (agent.browser?.history_error_code === 'history_dependency_wait') return {
    label: 'Waiting', detail: 'History collection is temporarily waiting. It will retry automatically.', tone: 'info',
  };
  if (agent.browser?.history_error_code === 'software_update_required') return {
    label: 'Update needed', detail: 'History collection is waiting for a compatible app update.', tone: 'warning',
  };
  if (agent.browser?.history_error_code === 'unsupported_revision') return {
    label: 'Needs attention', detail: 'The last history check could not read this site version. Check for an app update.', tone: 'warning',
  };
  if (coverage.phase === 'blocked' || agent.degraded_reason) return {
    label: 'Incomplete', detail: 'Older message collection has not completed.', tone: 'warning',
  };
  if (coverage.phase === 'not_started' || coverage.phase === 'paused') return {
    label: 'Waiting', detail: coverage.phase === 'paused' ? 'Waiting for older message collection to resume.'
      : 'Older message collection has not started.', tone: 'info',
  };
  return { label: 'Adding messages', detail: coverage.phase === 'discovering'
    ? 'Finding conversations with older messages.' : 'Adding older messages to this computer.', tone: 'info' };
}
