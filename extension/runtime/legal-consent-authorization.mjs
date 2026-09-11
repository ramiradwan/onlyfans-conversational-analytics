import { MODE_EVIDENCE_RECORD_SCHEMA, validateActivationEnvelopeV2 } from './activation-evidence.mjs';
import { authorizationScope, LEGAL_INSTRUMENT_NAMES, validateLegalInstrumentBindings } from './legal-instruments.mjs';
import { legalReleaseBindings } from './legal-release-bindings.mjs';

export { authorizationScope } from './legal-instruments.mjs';
// Obsolete storage is retained only for migration/deletion; it is never authority.
export const LEGAL_AUTHORIZATION_STORAGE_KEY = 'ofca_legal_authorization_v1';
export const LEGAL_AUTHORIZATION_SCHEMA = 'ofca-legal-authorization/v1';

export function modeRecordAuthorizes(record, mode, bindings) {
  try {
    if (record?.schema !== MODE_EVIDENCE_RECORD_SCHEMA || record.record_type !== 'mode_envelope') return false;
    const envelope = validateActivationEnvelopeV2(record.envelope);
    const binding = validateLegalInstrumentBindings(bindings);
    return envelope.event_id === record.event_id
      && envelope.selected_mode === mode
      && LEGAL_INSTRUMENT_NAMES.every((name) => ['version', 'rendered_sha256', 'public_url', 'locale']
        .every((key) => envelope.presented_instruments[name][key] === binding.instruments[name][key]))
      && record.authorization_scope === authorizationScope(bindings, mode);
  } catch {
    return false;
  }
}

export class LegalConsentAuthorization {
  constructor({ evidenceStore, bindings = legalReleaseBindings }) {
    if (typeof evidenceStore?.event !== 'function' || typeof bindings !== 'function') {
      throw new TypeError('Legal consent authorization requires evidence and release bindings');
    }
    this.evidenceStore = evidenceStore;
    this.bindings = bindings;
  }

  async recordAuthorizes(eventId, mode) {
    if (typeof eventId !== 'string') return false;
    try {
      const record = await this.evidenceStore.event(eventId);
      return record?.event_id === eventId && modeRecordAuthorizes(record, mode, this.bindings());
    } catch {
      return false;
    }
  }

  currentScope(mode) {
    try { return authorizationScope(this.bindings(), mode); } catch { return null; }
  }

  async authorizeTransition({ currentState, requestedMode, evidenceEventId = null }) {
    if (!['preview', 'full'].includes(requestedMode)) return true;
    const eventId = evidenceEventId ?? (currentState?.mode === requestedMode
      ? currentState.authorization_event_id : null);
    return this.recordAuthorizes(eventId, requestedMode);
  }

  async authorizeResume({ resumeMode, currentState }) {
    return currentState?.mode === 'paused'
      && this.recordAuthorizes(currentState.authorization_event_id, resumeMode);
  }

  async reconcileActiveMode({ mode, state }) {
    return this.recordAuthorizes(state?.authorization_event_id, mode);
  }
}
