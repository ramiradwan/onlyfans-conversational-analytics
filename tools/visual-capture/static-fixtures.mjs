// Visual fixtures exercise production markup and the existing setup controller, never live data.
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { applyPopupState, POPUP_STATES } from '../../extension/qualification/popup-visual-fixtures.mjs';
import { deriveCustomerJourney } from '../../extension/runtime/customer-journey.mjs';
import { createProvisioningController } from '../../app/provisioning/provisioning.js';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
const read = (name) => readFile(new URL('../../' + name, import.meta.url), 'utf8');
const withoutScripts = (html) => html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');

export async function staticFixtures() {
  const popupHtml = withoutScripts(await read('extension/popup.html'));
  const journey = deriveCustomerJourney({ status: { consent: { mode: 'off' } } });
  const inactive = { tone: journey.tone, mode: 'Analytics off', badge: '', title: journey.title, body: journey.body };
  const popupStates = {
    ...POPUP_STATES,
    software_activation: { ...inactive, panel: 'pre-mode' },
    software_activation_ready: { ...inactive, panel: 'pre-mode', accepted: true },
    legal_unavailable: { ...inactive, panel: 'legal-unavailable' },
    mode_choice: { ...inactive, panel: 'mode-choice' },
    mode_choice_full: { ...inactive, panel: 'mode-choice', fullReview: true },
    full_review: { ...POPUP_STATES.preview, panel: 'mode-choice', fullReview: true, upgrade: true },
    connection: { ...POPUP_STATES.full_ready, view: 'connection' },
    manage: { ...POPUP_STATES.full_ready, view: 'manage' },
  };
  const fixtures = [];
  for (const [name, state] of Object.entries(popupStates)) {
    const dom = new JSDOM(popupHtml);
    const doc = dom.window.document;
    applyPopupState(state, doc);
    if (state.panel) {
      doc.getElementById(state.panel).classList.remove('hidden');
      for (const id of ['journey-card', 'preview-metrics', 'history-prompt']) doc.getElementById(id).classList.add('hidden');
    }
    if (state.fullReview) {
      doc.getElementById('preview-disclosure').classList.add('hidden');
      doc.getElementById('full-disclosure').classList.remove('hidden');
      if (state.upgrade) doc.getElementById('full-secondary').textContent = 'Keep Preview';
    }
    if (state.accepted) {
      for (const id of ['terms-accepted', 'risk-acknowledged']) {
        doc.getElementById(id).setAttribute('checked', '');
        doc.getElementById(id).disabled = true;
      }
      doc.getElementById('activate-software').disabled = false;
    }
    if (state.view === 'manage') {
      for (const id of ['pause', 'forget-companion', 'revoke']) doc.getElementById(id).classList.remove('hidden');
    }
    if (state.preview) {
      for (const [id, count] of Object.entries({ 'messages-count': 128, 'chats-count': 24, 'inbound-count': 80, 'outbound-count': 48 })) {
        doc.getElementById(id).textContent = String(count);
      }
    }
    fixtures.push({ surface: 'popup', name, html: dom.serialize(), pairing: state.view === 'pairing',
      widths: state.panel || name === 'preview' ? [390, 320] : [390] });
    dom.window.close();
  }
  const template = withoutScripts(await read('app/provisioning/provisioning.html'));
  const controllerSource = await read('app/provisioning/provisioning.js');
  const elementIds = [...controllerSource.matchAll(/(\w+): byId\('([^']+)'\)/g)];
  const setupStates = [
    ['connect', 'registration_required'], ['confirm', 'creator_confirmation_required'],
    ['approve', 'creator_approval_pending'], ['finish', 'finalization_ready'],
    ['invalid-code', 'registration_required'], ['link-unavailable', 'registration_required'],
    ['approval-unavailable', 'creator_approval_pending'], ['completed', null],
    ['recovery', 'recovery_required'],
    ['approval-pending', 'creator_approval_pending'], ['approval-offline', 'creator_approval_pending'],
    ['approval-unavailable-help', 'creator_approval_pending'],
  ];
  for (const [name, stage] of setupStates) {
    const linkAvailable = !name.includes('unavailable');
    const html = template.replaceAll('{{PROVISIONING_CSRF}}', 'visual-fixture')
      .replaceAll('{{PROVISIONING_EXTENSION_ID}}', 'a'.repeat(32))
      .replaceAll('{{HOSTED_ONBOARDING_URL}}', linkAvailable ? 'https://setup.example/' : '')
      .replaceAll('{{HOSTED_ONBOARDING_VISIBILITY}}', linkAvailable ? '' : 'hidden');
    const dom = new JSDOM(html, { url: 'http://localhost/provisioning' });
    const document = dom.window.document;
    const associated = ['creator_approval_pending', 'finalization_ready'].includes(stage);
    const payload = stage === null ? { state: 'configured_restart' } : {
      state: 'provisioning_ready', stage,
      association_request_id: associated ? 'fixture-association' : null,
      creator_account_id: associated ? 'fixture-account' : null,
    };
    const controller = createProvisioningController({ document,
      elements: Object.fromEntries(elementIds.map(([, name, id]) => [name, document.getElementById(id)])),
      fetch: async (path) => path.endsWith('/acquire')
        ? { ok: false, status: name === 'approval-offline' ? 503 : 409, json: async () => ({
          reason: name === 'approval-offline' ? 'hosted_unavailable' : 'binding_acquisition_unavailable',
        }) } : { ok: true, json: async () => payload },
      sendExtensionMessage: async () => ({ type: 'provisioning.identity.result', version: 1,
        authenticated_profile: { creator_account_id: 'fixture-account' } }),
    });
    await controller.start();
    if (['approval-pending', 'approval-offline'].includes(name)) await controller.acquireAssociation();
    if (name === 'approval-unavailable-help') document.querySelector('.recovery-help').open = true;
    if (name === 'invalid-code') {
      const field = document.getElementById('claim-package');
      field.value = 'invalid code'; field.textContent = field.value;
      await controller.submitClaim({ preventDefault() {} });
    }
    fixtures.push({ surface: 'provisioning', name, html: dom.serialize(), widths: [1440, 390] });
    dom.window.close();
  }
  return fixtures;
}
