import { noticeText, secureExternalUrl } from './surface-client.mjs';
import { journeyBadge, readinessLabels } from './presentation.mjs';
export const element = (id) => document.getElementById(id);
export const show = (id, visible) => element(id).classList.toggle('hidden', !visible);
export const text = (id, value) => { element(id).textContent = value; };

// Reveal lower-page navigation after the first result, in its final position.
// The heading and loading notice stay visible, including during recovery.
export function renderLoading(status, failed) {
  document.querySelector('main').dataset.initialized = String(Boolean(status) || failed);
  show('loading', !status && !failed);
  show('runtime-unavailable', !status && failed);
  if (!status) document.querySelector('main').removeAttribute('data-ready');
}

export function renderJourney(journey) {
  element('journey-card').dataset.tone = journey.tone;
  element('journey-card').dataset.journeyState = journey.id;
  text('journey-badge', journeyBadge(journey));
  text('journey-title', journey.title); text('journey-body', journey.body);
}
export function renderReadiness(model) {
  const labels = readinessLabels(model);
  text('delivery-status', labels.connection); text('activation-status', labels.activation); text('analysis-status', labels.analysis);
}
export function renderLegalLinks(legal) {
  const bindings = legal?.configured ? legal.bindings : null;
  const instruments = bindings?.instruments;
  for (const [selector, name] of [['#terms-link', 'terms_of_service'], ['#risk-link', 'risk_disclosure'],
    ['#privacy-link, .extension-privacy-link', 'extension_privacy_notice']]) {
    let url = null;
    try { if (instruments?.[name]) url = secureExternalUrl(new URL(instruments[name].public_url, bindings.public_origin).href); } catch {}
    for (const link of document.querySelectorAll(selector)) {
      link.setAttribute('aria-disabled', String(url === null));
      if (link.id === 'privacy-link') link.textContent = url ? 'Extension Privacy Notice' : 'Privacy information unavailable';
      if (url) { link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      else link.removeAttribute('href');
    }
  }
}

export function createPageActions(client, render) {
  let busy = false;
  function lock(id, locked) {
    element(id).dataset.locked = String(locked);
    element(id).disabled = busy || locked;
  }
  function setBusy(value) {
    busy = value;
    for (const control of document.querySelectorAll('button, input')) control.disabled = busy || control.dataset.locked === 'true';
    document.querySelector('main').setAttribute('aria-busy', String(busy));
  }
  function feedback(value, error = false) {
    text('feedback', value); element('feedback').dataset.error = String(error);
  }
  async function run(operation, success = '') {
    if (busy) return;
    const focused = document.activeElement;
    setBusy(true); feedback('');
    try {
      // Call before the first await: browser permission prompts need this click.
      await operation();
      await client.sync();
      if (success) feedback(success);
    } catch (error) {
      await client.sync();
      feedback(noticeText(error, 'That did not complete. Try again.'), true);
    } finally {
      setBusy(false); render(client.model);
      if (focused instanceof HTMLElement && (focused.getClientRects().length === 0 || focused.hasAttribute('disabled'))) {
        const target = [...document.querySelectorAll('h2, #journey-primary')].find((node) => node.getClientRects().length > 0);
        if (target) { target.setAttribute('tabindex', '-1'); target.focus({ preventScroll: true }); }
      }
    }
  }
  function bind(id, operation, success = '') {
    element(id).addEventListener('click', () => { void run(operation, success); });
  }
  return { lock, run, bind, feedback, get busy() { return busy; } };
}
