const fields = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, '', location.pathname);
const kind = /^\/text-analysis-acquisition\/(paid|preview)\/entry$/.exec(location.pathname)?.[1];
const code = fields.get('code');
const status = document.getElementById('status');
const unavailable = () => { status.textContent = 'This link is unavailable. Open a fresh link from the desktop app.'; };
if (!kind || fields.size !== 1 || !/^[A-Za-z0-9_-]{43}$/.test(code ?? '')) unavailable();
else fetch(`/api/v1/nlp/text-analysis-acquisition/${kind}/browser-entry/consume`, {
  method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
  headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
}).then(async response => {
  if (!response.ok || !response.body || response.headers.get('content-type')?.split(';')[0] !== 'application/json'
      || Number(response.headers.get('content-length')) > 4096) throw Error();
  const reader = response.body.getReader();
  const raw = new Uint8Array(4096);
  let size = 0;
  try {
    for (;;) {
      const item = await reader.read();
      if (item.done) break;
      if (size + item.value.byteLength > raw.length) { await reader.cancel(); throw Error(); }
      raw.set(item.value, size);
      size += item.value.byteLength;
    }
  } finally { reader.releaseLock(); }
  const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw.subarray(0, size)));
  if (!value || Object.keys(value).length !== 1 || typeof value.url !== 'string') throw Error();
  const target = new URL(value.url);
  const path = new RegExp(`^/onboarding/text-analysis-${kind}-acquisitions/[A-Za-z0-9_-]{43}$`);
  if (target.protocol !== 'https:' || target.username || target.password || target.search || target.hash
      || !path.test(target.pathname)) throw Error();
  location.replace(target.href);
}).catch(unavailable);
