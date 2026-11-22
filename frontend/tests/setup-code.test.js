import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import { afterEach, describe, expect, it, vi } from 'vitest';

const html = readFileSync('setup-code/index.html', 'utf8');
const shellCss = readFileSync('shared-setup/shell.css', 'utf8');
const source = readFileSync('setup-code/main.js', 'utf8').split(/\r?\n/u).filter(line => !line.startsWith('import ')).join('\n');
const journeyId = '11111111-1111-4111-8111-111111111111';
const session = () => ({ csrf_token: 'A'.repeat(43), expires_at: Date.now() / 1000 + 600,
  hosted_url: 'https://receiver.example/session', journey_id: journeyId,
  workspace_journey_id: '22222222-2222-4222-8222-222222222222' });
const jsonResponse = value => new Response(JSON.stringify(value), {
  headers: { 'content-type': 'application/json' },
});
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await new Promise(resolve => setTimeout(resolve, 0)); };

function mount({ setupCode = async () => {}, receiving = async () => 'none', fetchImpl } = {}) {
  const dom = new JSDOM(html, { url: `https://receiver.example/setup-code#journey=${journeyId}`, runScripts: 'outside-only' });
  const { window } = dom;
  const requests = [];
  const receivingOptions = [];
  const setupCodeCalls = [];
  const request = fetchImpl ?? (async (path, options) => {
    requests.push([path, options]);
    if (path === '/api/v1/setup-code/session') return jsonResponse(session());
    throw new Error('unexpected request');
  });
  vi.stubGlobal('fetch', request);
  const run = new Function('document', 'window', 'location', 'history', 'AbortSignal',
    'URLSearchParams', 'parseOnboardingJson', 'applyTheme', 'continueReceivingTransfer', 'startReceivingCode',
    `${source}\n//# sourceURL=setup-code-main.js`);
  run(window.document, window, window.location, window.history, AbortSignal, URLSearchParams,
    value => JSON.parse(value), () => {}, options => { receivingOptions.push(options); return receiving(); },
    options => { setupCodeCalls.push(options); return setupCode(options); });
  return { dom, window, requests, receivingOptions, setupCodeCalls };
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('setup code entry states', () => {
  it('keeps guidance present while opening becomes ready', async () => {
    const page = mount();
    const { document } = page.window;
    expect(document.querySelector('#receiving-title').textContent).toBe('Use a setup code');
    expect(document.querySelector('#receiving-guidance').textContent)
      .toBe('Enter the code shown on your other computer or browser.');
    expect(document.querySelector('#receiving-status').textContent).toBe('Getting ready for your code…');
    expect(document.querySelector('#receiving-form').hidden).toBe(true);
    await settle();
    expect(document.querySelector('#receiving-form').hidden).toBe(false);
    expect(document.querySelector('label[for="receiving-code"]').textContent)
      .toBe('Code from the other computer or browser');
    expect(document.querySelector('#receiving-submit').textContent).toBe('Continue');
    expect(document.querySelector('#receiving-status').textContent).toBe('');
    expect(document.querySelector('#receiving-status'))
      .toBe(document.querySelector('.setup-card').lastElementChild);
    page.window.close();
  });

  it('refuses invalid input and shows a same-height checking action for one authorized request', async () => {
    let resolveRequest;
    const page = mount({ setupCode: () => new Promise(resolve => { resolveRequest = resolve; }) });
    const { document } = page.window;
    await settle();
    const form = document.querySelector('#receiving-form');
    const code = document.querySelector('#receiving-code');
    const button = document.querySelector('#receiving-submit');
    const submit = () => form.dispatchEvent(new page.window.Event('submit', { bubbles: true, cancelable: true }));
    code.value = 'INVALID!';
    submit();
    await settle();
    expect(page.requests.filter(([path]) => path === '/api/v1/setup-code/consume')).toHaveLength(0);
    expect(document.querySelector('#receiving-status').textContent).toBe('Enter the 12-character setup code.');
    code.value = '0123456789ab';
    submit();
    await settle();
    expect(page.receivingOptions).toHaveLength(1);
    expect(page.setupCodeCalls).toHaveLength(1);
    expect(page.setupCodeCalls[0].setupCode).toBe('0123456789AB');
    expect(page.setupCodeCalls[0].csrfToken).toBe('A'.repeat(43));
    expect(form.hidden).toBe(false);
    expect(code.value).toBe('');
    expect(button.textContent).toBe('Checking the code…');
    expect(button.disabled).toBe(true);
    expect(button.parentElement.classList.contains('setup-actions')).toBe(true);
    expect(shellCss).toMatch(/\.setup-code-card \.setup-actions > button \{ min-block-size: var\(--dipsy-static-control-height\); \}/u);
    expect(document.querySelector('#receiving-guidance').isConnected).toBe(true);
    expect(document.querySelector('#receiving-status').textContent).toBe('Checking the code…');
    submit();
    await settle();
    expect(page.receivingOptions).toHaveLength(1);
    expect(page.setupCodeCalls).toHaveLength(1);
    resolveRequest();
    await settle();
    page.window.close();
  });

  it('expires the session in place and clears the sensitive field', async () => {
    const page = mount({ fetchImpl: async path => path === '/api/v1/setup-code/session'
      ? jsonResponse({ ...session(), expires_at: Date.now() / 1000 - 1 }) : jsonResponse({}) });
    await settle();
    const { document } = page.window;
    expect(document.querySelector('#receiving-form').hidden).toBe(true);
    expect(document.querySelector('#receiving-code').value).toBe('');
    expect(document.querySelector('#receiving-status').textContent)
      .toBe('This receiving session expired. Open a fresh link in the desktop app.');
    expect(document.querySelector('#receiving-actions').children).toHaveLength(0);
    page.window.close();
  });

  it('resumes from BFCache as a read-only receiving session', async () => {
    const page = mount();
    await settle();
    const { window } = page;
    const code = window.document.querySelector('#receiving-code');
    code.value = '0123456789AB';
    window.dispatchEvent(new window.PageTransitionEvent('pagehide'));
    expect(code.value).toBe('');
    window.dispatchEvent(new window.PageTransitionEvent('pageshow', { persisted: true }));
    await settle();
    expect(page.receivingOptions.map(options => options.readOnly)).toEqual([false, true]);
    expect(page.requests.filter(([path]) => path === '/api/v1/setup-code/session')).toHaveLength(2);
    window.close();
  });
});







