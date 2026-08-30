const ACTIVE_MODES = new Set(['preview', 'full']);

function activeMode(value) {
  return ACTIVE_MODES.has(value);
}

export class LegalConsentAuthorization {
  constructor({ evidenceStore }) {
    if (
      typeof evidenceStore?.event !== 'function'
      || typeof evidenceStore?.modeEvidenceExists !== 'function'
    ) {
      throw new TypeError('Legal consent authorization requires an evidence store');
    }
    this.evidenceStore = evidenceStore;
  }

  async authorizeTransition({ currentState, requestedMode, evidenceEventId = null }) {
    if (!activeMode(requestedMode)) return true;

    // Re-applying the same active mode after the browser permission was restored
    // is not a new Legal choice. Existing persisted evidence is sufficient.
    if (
      currentState?.mode === requestedMode
      && await this.evidenceStore.modeEvidenceExists(requestedMode)
    ) return true;

    if (typeof evidenceEventId !== 'string' || evidenceEventId.length === 0) return false;
    const record = await this.evidenceStore.event(evidenceEventId);
    return record?.record_type === 'mode_envelope'
      && record.envelope?.selected_mode === requestedMode;
  }

  async authorizeResume({ resumeMode }) {
    if (!activeMode(resumeMode)) return false;
    return this.evidenceStore.modeEvidenceExists(resumeMode);
  }

  async reconcileActiveMode({ mode }) {
    if (!activeMode(mode)) return true;
    return this.evidenceStore.modeEvidenceExists(mode);
  }
}
