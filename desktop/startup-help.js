channels => {
  if (window !== window.top || ![
    'http://tauri.localhost/data-recovery.html',
    'http://tauri.localhost/unavailable.html',
  ].includes(location.href)) return;
  const render = () => {
    const card = document.querySelector('.startup-card');
    if (!card || card.querySelector('[data-support-help]')) return;
    const list = document.createElement('ul');
    for (const [key, name] of [['whatsapp', 'WhatsApp'], ['telegram', 'Telegram'], ['tiktok', 'TikTok']]) {
      if (!Object.hasOwn(channels, key) || !channels[key]) continue;
      const row = document.createElement('li');
      const address = document.createElement('span');
      address.textContent = channels[key];
      row.append(name + ' ', address);
      list.append(row);
    }
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
