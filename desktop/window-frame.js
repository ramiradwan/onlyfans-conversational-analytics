// Native-owned presentation only. Browser documents never load this script.
(() => {
  const shell = location.origin === 'http://tauri.localhost'
    && ['/', '/index.html', '/unavailable.html'].includes(location.pathname);
  const local = location.origin === 'http://bridge.localhost:17871'
    && (['/', '/onboarding', '/provisioning', '/api/v1/session/handoff'].includes(location.pathname)
      || location.pathname.startsWith('/onboarding/') || location.pathname.startsWith('/provisioning/'));
  if (window !== window.top || !(shell || local)) return;

  const invoke = (action) => window.__TAURI__.core.invoke('desktop_window', { action });
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(`
    :host { display: inline-flex; align-self: center; flex: 0 0 auto; margin-inline-start: auto; color: inherit; }
    button { appearance: none; display: grid; place-items: center; width: 44px; height: 40px;
      margin: 0; padding: 0; border: 0; border-radius: 6px; background: transparent; color: inherit; cursor: default; }
    button:hover { background: color-mix(in srgb, currentColor 10%, transparent); }
    button:active { background: color-mix(in srgb, currentColor 18%, transparent); }
    button:focus-visible { outline: 2px solid currentColor; outline-offset: -4px; }
    button[data-action="close"]:hover { background: #c42b1c; color: white; }
    svg { width: 12px; height: 12px; fill: none; stroke: currentColor; stroke-width: 1; pointer-events: none; }
    .status { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
    @media (forced-colors: active) { button { border: 1px solid ButtonText; } }
  `);
  const frameSheet = new CSSStyleSheet();
  frameSheet.replaceSync(`
    [data-desktop-drag-region] { user-select: none; }
    #desktop-fallback-header { display: flex; align-items: center; gap: 16px; padding: 12px 16px 12px 28px;
      background: Canvas; color: CanvasText; border-bottom: 1px solid GrayText; font: 600 16px system-ui, sans-serif; }
  `);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, frameSheet];
  const icons = {
    minimize: '<path d="M1 6.5h10"/>',
    maximize: '<rect x="1.5" y="1.5" width="9" height="9"/>',
    restore: '<path d="M3.5 2V.5h8v8H10"/><rect x=".5" y="3.5" width="8" height="8"/>',
    close: '<path d="m1 1 10 10M1 11 11 1"/>',
  };
  const icon = (name) => `<svg aria-hidden="true" viewBox="0 0 12 12">${icons[name]}</svg>`;
  class WindowControls extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: 'open' });
      root.adoptedStyleSheets = [sheet];
      root.innerHTML = `<button type="button" data-action="minimize" aria-label="Minimize" title="Minimize">${icon('minimize')}</button>
        <button type="button" data-action="toggle_maximize" aria-label="Maximize" title="Maximize">${icon('maximize')}</button>
        <button type="button" data-action="close" aria-label="Close window" title="Close window">${icon('close')}</button>
        <span class="status" role="status"></span>`;
      root.addEventListener('click', async (event) => {
        const button = event.target.closest('button');
        if (!button) return;
        try { this.update(await invoke(button.dataset.action)); }
        catch { root.querySelector('.status').textContent = 'Window action failed.'; }
      });
      this.refresh = () => { void invoke('state').then((state) => this.update(state)).catch(() => {}); };
    }
    connectedCallback() {
      if (!this.closest('#desktop-fallback-header')) document.getElementById('desktop-fallback-header')?.remove();
      window.addEventListener('resize', this.refresh);
      window.addEventListener('focus', this.refresh);
      this.refresh();
    }
    disconnectedCallback() {
      window.removeEventListener('resize', this.refresh);
      window.removeEventListener('focus', this.refresh);
      queueMicrotask(ensureFrame);
    }
    update(maximized) {
      const button = this.shadowRoot.querySelector('[data-action="toggle_maximize"]');
      const label = maximized ? 'Restore' : 'Maximize';
      button.setAttribute('aria-label', label);
      button.title = label;
      button.innerHTML = icon(maximized ? 'restore' : 'maximize');
    }
  }
  function ensureFrame() {
    if (!document.body || document.querySelector('desktop-window-controls')) return;
    // Keep the window usable even if the local document cannot render its header.
    const header = document.createElement('header');
    header.id = 'desktop-fallback-header';
    header.setAttribute('data-desktop-drag-region', '');
    header.append('Conversation Analytics', document.createElement('desktop-window-controls'));
    document.body.prepend(header);
  }
  customElements.define('desktop-window-controls', WindowControls);
  document.addEventListener('mousedown', (event) => {
    const target = event.target;
    if (event.button !== 0 || !(target instanceof Element) || !target.closest('[data-desktop-drag-region]')
      || target.closest('button, a, input, select, textarea, summary, [role="button"], [contenteditable], desktop-window-controls, [data-desktop-no-drag]')) return;
    event.preventDefault();
    void invoke(event.detail === 2 ? 'toggle_maximize' : 'start_dragging').catch(() => {});
  });
  document.addEventListener('DOMContentLoaded', ensureFrame, { once: true });
})();
