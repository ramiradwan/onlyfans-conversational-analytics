import { PROVISIONING_STATES } from './static-fixture-matrix.mjs';
// Visual fixtures exercise production markup and the existing setup controller, never live data.
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { SURFACE_STATES, surfaceDocument, surfaceBundle } from '../../extension/qualification/surface-fixtures.mjs';
import { createProvisioningController } from '../../app/provisioning/provisioning.js';

const { JSDOM } = createRequire(new URL('../../frontend/package.json', import.meta.url))('jsdom');
const read = (name) => readFile(new URL('../../' + name, import.meta.url), 'utf8');
const withoutScripts = (html) => html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');

export async function staticFixtures() {
  const fixtures = [];
  for (const [name, state] of Object.entries(SURFACE_STATES)) {
    fixtures.push({ surface: state.surface, name, state,
      html: await surfaceDocument(state), script: await surfaceBundle(state.surface),
      widths: state.surface === 'popup' ? [390, 320] : [1440, 390] });
  }
  const template = withoutScripts(await read('app/provisioning/provisioning.html'));
  const controllerSource = await read('app/provisioning/provisioning.js');
  const elementIds = [...controllerSource.matchAll(/(\w+): byId\('([^']+)'\)/g)];
  for (const [name, stage] of PROVISIONING_STATES) {
    const linkAvailable = !name.includes('unavailable');
    const html = template.replaceAll('{{PROVISIONING_CSRF}}', 'visual-fixture')
      .replaceAll('{{PROVISIONING_EXTENSION_ID}}', 'a'.repeat(32))
      .replaceAll('{{HOSTED_ONBOARDING_URL}}', linkAvailable ? 'https://setup.example/' : '')
      .replaceAll('{{HOSTED_ONBOARDING_VISIBILITY}}', linkAvailable ? '' : 'hidden');
    const dom = new JSDOM(html, { url: 'http://localhost/provisioning', pretendToBeVisual: true });
    const document = dom.window.document;
    const associated = ['creator_approval_pending', 'finalization_ready'].includes(stage);
    const payload = stage === null ? { state: 'configured_restart' } : {
      state: 'provisioning_ready', stage,
      association_request_id: associated ? 'fixture-association' : null,
      creator_account_id: associated ? 'fixture-account' : null,
    };
    let beganFinalization;
    const finalizationPending = new Promise((resolve) => { beganFinalization = resolve; });
    const controller = createProvisioningController({ document,
      elements: Object.fromEntries(elementIds.map(([, name, id]) => [name, document.getElementById(id)])),
      fetch: async (path) => {
        if (path.endsWith('/finalize')) {
          beganFinalization();
          return new Promise(() => {});
        }
        if (path.endsWith('/acquire')) return { ok: false, status: name === 'approval-offline' ? 503 : 409, json: async () => ({
          reason: name === 'approval-offline' ? 'hosted_unavailable' : 'binding_acquisition_unavailable',
        }) };
        return { ok: true, json: async () => payload };
      },
      sendExtensionMessage: async () => ({ type: 'provisioning.identity.result', version: 1,
        authenticated_profile: { creator_account_id: 'fixture-account' } }),
    });
    const started = controller.start();
    if (stage === 'finalization_ready') await Promise.race([started, finalizationPending]);
    else await started;
    if (['approval-pending', 'approval-offline'].includes(name)) await controller.acquireAssociation();
    if (name === 'approval-unavailable-help') document.querySelector('#acquire-association').focus();
    if (name === 'invalid-code') {
      const field = document.getElementById('claim-package');
      field.value = 'invalid code'; field.textContent = field.value;
      await controller.submitClaim({ preventDefault() {} });
    }
    fixtures.push({ surface: 'provisioning', name, html: dom.serialize(), provisioning: { stage, name }, widths: [1440, 390] });
    dom.window.close();
  }
  return fixtures;
}
