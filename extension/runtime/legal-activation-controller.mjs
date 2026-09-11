import { validateLegalInstrumentBindings } from './legal-instruments.mjs';
import { SerialExecutor } from './operation-scope.mjs';
import {
  authorizationScope,
  modeRecordAuthorizes,
} from './legal-consent-authorization.mjs';

export const LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE = 'ofca.legal-activation.status';
export const LEGAL_ACCEPT_TERMS_MESSAGE_TYPE = 'ofca.legal-activation.accept-terms';
export const LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE = 'ofca.legal-activation.acknowledge-risk';
export const LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE = 'ofca.legal-activation.activate-software';
export const LEGAL_CHOOSE_MODE_MESSAGE_TYPE = 'ofca.legal-activation.choose-mode';
export const LEGAL_AUDIT_EXPORT_MESSAGE_TYPE = 'ofca.legal-activation.audit-export';
export const LEGAL_ACTIVATION_FLOW_STORAGE_KEY = 'ofca_legal_activation_flow_v1';

const ACTIVE_MODES = new Set(['preview', 'full']);
const LEGAL_EVENT_TYPES = new Set(['initial_activation', 'mode_upgrade', 'reauthorization', 'terms_reacceptance']);

const freshFlow = ({ termsEventId = null, riskEventId = null, stage = 'pre_mode', bindingScope = null, termsReacceptanceRequired = false } = {}) => ({
  schema: 'ofca-legal-activation-flow/v1',
  binding_scope: bindingScope,
  terms_reacceptance_required: termsReacceptanceRequired,
  transaction_id: crypto.randomUUID(),
  terms_event_id: termsEventId,
  risk_event_id: riskEventId,
  stage,
  pending_mode: null,
  pending_event_type: null,
  completed_mode: null,
  completed_event_id: null,
});

function trustedUiSender(sender, chromeApi) {
  return sender?.id === chromeApi.runtime.id
    && typeof sender?.url === 'string'
    && sender.url.startsWith(chromeApi.runtime.getURL(''));
}

function validFlow(value) {
  if (
    typeof value !== 'object'
    || value === null
    || value.schema !== 'ofca-legal-activation-flow/v1'
    || typeof value.transaction_id !== 'string'
    || !['pre_mode', 'mode_selection'].includes(value.stage)
  ) return freshFlow();

  const pendingMode = ACTIVE_MODES.has(value.pending_mode) ? value.pending_mode : null;
  const pendingEventType = LEGAL_EVENT_TYPES.has(value.pending_event_type)
    ? value.pending_event_type
    : null;
  return {
    schema: value.schema,
    binding_scope: typeof value.binding_scope === 'string' ? value.binding_scope : null,
    terms_reacceptance_required: value.terms_reacceptance_required === true,
    transaction_id: value.transaction_id,
    terms_event_id: typeof value.terms_event_id === 'string' ? value.terms_event_id : null,
    risk_event_id: typeof value.risk_event_id === 'string' ? value.risk_event_id : null,
    stage: value.stage,
    pending_mode: pendingMode,
    pending_event_type: pendingMode === null ? null : pendingEventType,
    completed_mode: ACTIVE_MODES.has(value.completed_mode) ? value.completed_mode : null,
    completed_event_id: typeof value.completed_event_id === 'string'
      ? value.completed_event_id
      : null,
  };
}

function sameInstrument(left, right) {
  return ['version', 'rendered_sha256', 'public_url', 'locale']
    .every((key) => left?.[key] === right?.[key]);
}

function preModeEventMatches(record, meaning, instrument) {
  return record?.record_type === 'pre_mode'
    && record.legal_meaning === meaning
    && sameInstrument(record.instrument, instrument);
}

export class LegalActivationController {
  constructor({ chromeApi = globalThis.chrome, consentController, evidenceStore, bindings }) {
    if (!chromeApi?.runtime?.onMessage || !chromeApi?.storage?.local) {
      throw new TypeError('Legal activation controller requires Chrome runtime/storage');
    }
    if (typeof consentController?.setMode !== 'function' || typeof consentController?.status !== 'function') {
      throw new TypeError('Legal activation controller requires a consent controller');
    }
    if (typeof evidenceStore?.event !== 'function') {
      throw new TypeError('Legal activation controller requires an evidence store');
    }
    if (typeof bindings !== 'function') {
      throw new TypeError('Legal activation controller requires a release-binding provider');
    }
    this.chromeApi = chromeApi;
    this.consentController = consentController;
    this.evidenceStore = evidenceStore;
    this.bindings = bindings;
    this.queue = new SerialExecutor();
    this.registered = false;
    this.listener = this.#onMessage.bind(this);
  }

  register() {
    if (this.registered) return;
    this.chromeApi.runtime.onMessage.addListener(this.listener);
    this.registered = true;
  }

  #binding() {
    const candidate = this.bindings();
    return candidate === null ? null : validateLegalInstrumentBindings(candidate);
  }

  async #storedFlow() {
    const saved = await this.chromeApi.storage.local.get([LEGAL_ACTIVATION_FLOW_STORAGE_KEY]);
    return validFlow(saved?.[LEGAL_ACTIVATION_FLOW_STORAGE_KEY]);
  }

  async #flow(binding) {
    const flow = await this.#storedFlow();
    if (binding === null) return flow;
    const scope = authorizationScope(binding, 'preview');
    if (flow.binding_scope === scope) return flow;
    const [terms, risk] = await Promise.all([
      flow.terms_event_id === null ? null : this.evidenceStore.event(flow.terms_event_id),
      flow.risk_event_id === null ? null : this.evidenceStore.event(flow.risk_event_id),
    ]);
    const termsCurrent = preModeEventMatches(terms, 'terms', binding.instruments.terms_of_service);
    const riskCurrent = preModeEventMatches(risk, 'risk_disclosure', binding.instruments.risk_disclosure);
    return this.#saveFlow(freshFlow({
      termsEventId: termsCurrent ? flow.terms_event_id : null,
      riskEventId: riskCurrent ? flow.risk_event_id : null,
      stage: termsCurrent && riskCurrent ? 'mode_selection' : 'pre_mode',
      bindingScope: scope,
      termsReacceptanceRequired: flow.terms_event_id !== null && !termsCurrent,
    }));
  }

  async #saveFlow(flow) {
    await this.chromeApi.storage.local.set({ [LEGAL_ACTIVATION_FLOW_STORAGE_KEY]: flow });
    return structuredClone(flow);
  }

  #run(work) {
    if (typeof this.consentController.runLegalOperation === 'function') {
      return this.consentController.runLegalOperation(work);
    }
    return this.queue.run(() => work({
      status: () => this.consentController.status(),
      setMode: (mode, options) => this.consentController.setMode(mode, options),
      assertCurrent: () => {},
    }));
  }

  status() { return this.#run((context) => this.#statusLocked(context)); }
  acceptTerms() { return this.#run((context) => this.#acceptTermsLocked(context)); }
  acknowledgeRisk() { return this.#run((context) => this.#acknowledgeRiskLocked(context)); }
  activateSoftware() { return this.#run((context) => this.#activateSoftwareLocked(context)); }
  chooseMode(mode) { return this.#run((context) => this.#chooseModeLocked(mode, context)); }
  exportAuditTrail() { return this.#run(() => this.evidenceStore.exportAuditTrail()); }

  async #statusLocked(context) {
    const binding = this.#binding();
    const flow = await this.#flow(binding);
    const consent = await context.status();
    const resumeMode = consent.consent.mode === 'paused'
      && ACTIVE_MODES.has(consent.consent.resume_mode)
      ? consent.consent.resume_mode
      : null;
    return {
      schema: 'ofca-legal-activation-status/v1',
      configured: binding !== null,
      bindings: binding === null ? null : {
        public_origin: binding.public_origin,
        instruments: structuredClone(binding.instruments),
      },
      flow,
      consent_mode: consent.consent.mode,
      requires_reauthorization: resumeMode !== null
        && !modeRecordAuthorizes(
          consent.consent.authorization_event_id
            ? await this.evidenceStore.event(consent.consent.authorization_event_id) : null,
          resumeMode, binding,
        ),
    };
  }

  async #acceptTermsLocked(context) {
    const binding = this.#binding();
    if (binding === null) throw new Error('Legal instrument bindings are not configured');
    const flow = await this.#flow(binding);
    const record = await this.evidenceStore.recordTermsAcceptance({
      transactionId: flow.transaction_id,
      bindings: binding,
    });
    flow.terms_event_id = record.event_id;
    await this.#saveFlow(flow);
    context.assertCurrent();
    return this.#statusLocked(context);
  }

  async #acknowledgeRiskLocked(context) {
    const binding = this.#binding();
    if (binding === null) throw new Error('Legal instrument bindings are not configured');
    const flow = await this.#flow(binding);
    const record = await this.evidenceStore.recordRiskAcknowledgment({
      transactionId: flow.transaction_id,
      bindings: binding,
    });
    flow.risk_event_id = record.event_id;
    await this.#saveFlow(flow);
    context.assertCurrent();
    return this.#statusLocked(context);
  }

  async #activateSoftwareLocked(context) {
    const binding = this.#binding();
    if (binding === null) throw new Error('Legal instrument bindings are not configured');
    const flow = await this.#flow(binding);
    if (flow.terms_event_id === null || flow.risk_event_id === null) {
      throw new Error('Terms and risk actions must be completed first');
    }
    flow.stage = 'mode_selection';
    await this.#saveFlow(flow);
    context.assertCurrent();
    return this.#statusLocked(context);
  }

  async #chooseModeLocked(mode, context) {
    if (!ACTIVE_MODES.has(mode)) throw new Error('Legal mode choice must be preview or full');
    const binding = this.#binding();
    if (binding === null) throw new Error('Legal instrument bindings are not configured');
    let flow = await this.#flow(binding);
    if (
      flow.stage !== 'mode_selection'
      || flow.terms_event_id === null
      || flow.risk_event_id === null
    ) {
      throw new Error('Activate Software must complete before mode choice');
    }

    const consent = (await context.status()).consent;
    if (
      flow.completed_mode === mode
      && flow.completed_event_id !== null
      && consent.mode === mode
    ) {
      const prior = await this.evidenceStore.event(flow.completed_event_id);
      if (!modeRecordAuthorizes(prior, mode, binding)) {
        throw new Error('Saved Legal mode-choice retry state is inconsistent');
      }
      const status = await context.setMode(mode, {
        evidenceEventId: prior.event_id,
      });
      return { status, evidence: structuredClone(prior.envelope), retried: true };
    }

    if (flow.pending_mode !== null && flow.pending_mode !== mode) {
      throw new Error('A different Legal mode choice is already pending');
    }

    if (flow.pending_mode === null) {
      let eventType = 'initial_activation';
      if (flow.terms_reacceptance_required) {
        eventType = 'terms_reacceptance';
      } else if (consent.mode === 'preview' && mode === 'full') {
        eventType = 'mode_upgrade';
        if (flow.completed_mode === 'preview') {
          flow = freshFlow({
            termsEventId: flow.terms_event_id,
            riskEventId: flow.risk_event_id,
            stage: 'mode_selection',
            bindingScope: flow.binding_scope,
          });
        }
      } else if (consent.mode === 'revoked' || consent.mode === 'paused') {
        eventType = 'reauthorization';
        if (flow.completed_mode !== null) {
          flow = freshFlow({
            termsEventId: flow.terms_event_id,
            riskEventId: flow.risk_event_id,
            stage: 'mode_selection',
            bindingScope: flow.binding_scope,
          });
        }
      }
      flow.pending_mode = mode;
      flow.pending_event_type = eventType;
      await this.#saveFlow(flow);
    }

    if (!LEGAL_EVENT_TYPES.has(flow.pending_event_type)) {
      throw new Error('Saved Legal mode-choice event type is inconsistent');
    }
    const record = await this.evidenceStore.recordModeChoice({
      transactionId: flow.transaction_id,
      eventType: flow.pending_event_type,
      selectedMode: mode,
      termsEventId: flow.terms_event_id,
      riskEventId: flow.risk_event_id,
      bindings: binding,
    });
    const status = await context.setMode(mode, {
      evidenceEventId: record.event_id,
    });
    flow.pending_mode = null;
    flow.pending_event_type = null;
    flow.completed_mode = mode;
    flow.completed_event_id = record.event_id;
    await this.#saveFlow(flow);
    return { status, evidence: record.envelope, retried: false };
  }

  #onMessage(message, sender, sendResponse) {
    if (!trustedUiSender(sender, this.chromeApi)) return false;
    const calls = new Map([
      [LEGAL_ACTIVATION_STATUS_MESSAGE_TYPE, () => this.status()],
      [LEGAL_ACCEPT_TERMS_MESSAGE_TYPE, () => this.acceptTerms()],
      [LEGAL_ACKNOWLEDGE_RISK_MESSAGE_TYPE, () => this.acknowledgeRisk()],
      [LEGAL_ACTIVATE_SOFTWARE_MESSAGE_TYPE, () => this.activateSoftware()],
      [LEGAL_AUDIT_EXPORT_MESSAGE_TYPE, () => this.exportAuditTrail()],
    ]);
    if (calls.has(message?.type) && Object.keys(message).length === 1) {
      void calls.get(message.type)().then(
        (result) => sendResponse({ ok: true, result }),
        (error) => sendResponse({ ok: false, code: error.message }),
      );
      return true;
    }
    if (
      message?.type === LEGAL_CHOOSE_MODE_MESSAGE_TYPE
      && Object.keys(message).length === 2
      && ACTIVE_MODES.has(message.mode)
    ) {
      void this.chooseMode(message.mode).then(
        (result) => sendResponse({ ok: true, result }),
        (error) => sendResponse({ ok: false, code: error.message }),
      );
      return true;
    }
    return false;
  }
}
