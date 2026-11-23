const labels = { whatsapp: 'WhatsApp', telegram: 'Telegram', tiktok: 'TikTok' };

export function renderSupportHelp(slot, eligible) {
  const document = slot.ownerDocument;
  const existing = slot.querySelector(':scope > [data-support-help]');
  const source = eligible ? document.getElementById('support-channels')?.content : null;
  if (!source?.children.length) {
    existing?.remove();
    return;
  }
  if (existing) return;
  const entries = document.createElement('ul');
  for (const [channel, label] of Object.entries(labels)) {
    const address = source.querySelector(`span[data-channel="${channel}"]`)?.textContent;
    if (!address) continue;
    const row = document.createElement('li');
    const value = document.createElement('span');
    value.textContent = address;
    row.append(label + ' ', value);
    entries.append(row);
  }
  if (!entries.children.length) return;
  const block = document.createElement('div');
  block.dataset.supportHelp = '';
  const heading = document.createElement('h2');
  heading.textContent = 'Get help';
  block.append(heading, entries);
  slot.append(block);
}
