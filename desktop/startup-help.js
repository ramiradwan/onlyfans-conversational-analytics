channels => {
  if (window !== window.top) return;
  const contactList = () => {
    const list = document.createElement('ul');
    for (const [key, name] of [['whatsapp', 'WhatsApp'], ['telegram', 'Telegram'], ['tiktok', 'TikTok']]) {
      if (!Object.hasOwn(channels, key) || !channels[key]) continue;
      const row = document.createElement('li');
      const address = document.createElement('span');
      address.textContent = channels[key];
      row.append(name + ' ', address);
      list.append(row);
    }
    return list;
  };
  const terminal = [
    'http://tauri.localhost/data-recovery.html',
    'http://tauri.localhost/unavailable.html',
  ].includes(location.href);
  if (!terminal) {
    // Only a desktop-owned document has the native window controls.
    if (!customElements.get('desktop-window-controls')) return;
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(`
      :host { align-self: center; color: inherit; font: inherit; }
      button { padding: .4em .65em; border: 0; border-radius: .35em; background: transparent;
        color: inherit; font: inherit; cursor: pointer; }
      button:hover { background: color-mix(in srgb, currentColor 10%, transparent); }
      button:focus-visible { outline: 2px solid currentColor; }
      [popover] { box-sizing: border-box; max-inline-size: min(29rem, calc(100vw - 2rem));
        max-block-size: calc(100vh - 2rem); overflow: auto; padding: 1.25rem; border: 1px solid GrayText;
        border-radius: .6rem; background: Canvas; color: CanvasText; font: 1rem/1.5 system-ui, sans-serif;
        text-align: start; user-select: text; overflow-wrap: anywhere; }
      h2 { margin: 0 0 .65rem; font-size: 1.15rem; } ul { padding: 0; margin: 0 0 .8rem; list-style: none; }
      li + li { margin-block-start: .4rem; } [popover] button { border: 1px solid GrayText; }
    `);
    customElements.define('desktop-help', class extends HTMLElement {
      constructor() {
        super();
        const root = this.attachShadow({ mode: 'open' });
        root.adoptedStyleSheets = [sheet];
        root.innerHTML = `<button type="button" popovertarget="contacts" aria-haspopup="dialog">Get help</button>
          <section id="contacts" popover role="dialog" aria-labelledby="heading">
            <h2 id="heading">Get help</h2>
            <button type="button" popovertarget="contacts" popovertargetaction="hide">Close</button>
          </section>`;
        root.querySelector('h2').after(contactList());
      }
    });
    return;
  }
  const render = () => {
    const card = document.querySelector('.startup-card');
    if (!card || card.querySelector('[data-support-help]')) return;
    const list = contactList();
    if (!list.children.length) return;
    const block = document.createElement('div');
    block.dataset.supportHelp = '';
    const heading = document.createElement('h2');
    heading.textContent = 'Get help';
    block.append(heading, list);
    card.append(block);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', render, { once: true });
  else render();
}
