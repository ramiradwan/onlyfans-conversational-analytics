import { normalizeSetupCode, SETUP_TRANSFER_MESSAGE, canonicalTransfer } from '../runtime/setup-transfer.mjs';
import { onboardingHostedOrigin } from '../runtime/onboarding-release-config.mjs';

// The transferred target is a workflow hint. It never marks an account verified.
export function renderReceivingContext(document, context) {
  const region = document.getElementById('setup-transfer-creator');
  if (!region) return;
  const target = context?.intended_creator_id;
  region.classList.toggle('hidden', !target);
  if (!target) return;
  document.getElementById('setup-transfer-intended').textContent = 'Account name unavailable';
  document.getElementById('setup-transfer-current').textContent = context.current_creator_id ? 'Account name unavailable' : 'Not identified';
  document.getElementById('setup-transfer-mismatch').classList.toggle('hidden',
    !context.current_creator_id || context.current_creator_id === target);
}

export function submitReceivingEntry(document, payload, hostedOrigin = onboardingHostedOrigin) {
  if (!hostedOrigin || payload?.hosted_start_url !== `${hostedOrigin}/public/onboarding/setup/receive`
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u.test(payload.journey_id)
    || payload.request?.destination?.kind !== 'browser-extension') throw new Error('receiving_entry_unavailable');
  const form = document.createElement('form');
  form.method = 'post'; form.target = '_self'; form.action = payload.hosted_start_url; form.hidden = true;
  for (const [name, value] of Object.entries({ journey_id: payload.journey_id, request: canonicalTransfer(payload.request) })) {
    const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value; form.append(input);
  }
  document.body.append(form); form.submit();
}

export function bindSetupTransfer({ document, send, hostedOrigin = onboardingHostedOrigin }) {
  const button = document.getElementById('setup-transfer-open');
  if (!hostedOrigin || !button) return;
  button.classList.remove('hidden');
  const form = document.getElementById('setup-transfer-form');
  const input = document.getElementById('setup-transfer-code');
  const status = document.getElementById('setup-transfer-status');
  const submit = document.getElementById('setup-transfer-submit');
  const region = document.getElementById('setup-transfer-entry');
  const normal = ['setup-rail', 'extension-stage', 'surface-feedback'].map((id) => document.querySelector(`[data-reserved-region="${id}"]`));
  let busy = false;
  const setOpen = (open) => {
    region.classList.toggle('hidden', !open);
    button.classList.toggle('hidden', open);
    normal.forEach((item) => item?.classList.toggle('hidden', open));
    (open ? input : button).focus();
  };
  button.addEventListener('click', () => setOpen(true));
  document.getElementById('setup-transfer-back').addEventListener('click', () => { if (!busy) setOpen(false); });
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); if (busy) return;
    const code = normalizeSetupCode(input.value);
    if (!code) { status.textContent = 'Enter the 12-character setup code.'; input.setAttribute('aria-invalid', 'true'); input.focus(); return; }
    input.removeAttribute('aria-invalid'); busy = true; submit.disabled = true; status.textContent = 'Opening setup…';
    try {
      const payload = await send({ type: SETUP_TRANSFER_MESSAGE, action: 'prepare', setup_code: code });
      submitReceivingEntry(document, payload, hostedOrigin);
    } catch { status.textContent = 'Setup could not be opened.'; }
    finally { busy = false; submit.disabled = false; }
  });
  return { open: () => setOpen(true) };
}
