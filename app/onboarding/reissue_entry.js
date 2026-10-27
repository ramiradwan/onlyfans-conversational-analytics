const fields = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, '', location.pathname);
const code = fields.get('code');
const status = document.getElementById('status');
const unavailable = () => { status.textContent = 'This link is unavailable. Open a fresh link from the desktop app.'; };
if (fields.size !== 1 || !/^[A-Za-z0-9_-]{43}$/.test(code ?? '')) unavailable();
else fetch('/api/v1/nlp/license-reissue/browser-entry/consume', {
  method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
  headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
}).then(async response => {
  if (!response.ok || !response.body || response.headers.get('content-type')?.split(';')[0] !== 'application/json'
      || Number(response.headers.get('content-length')) > 4096) throw Error();
  const reader = response.body.getReader(), chunks = [];
  let size = 0;
  try {
    for (;;) {
      const item = await reader.read();
      if (item.done) break;
      size += item.value.byteLength;
      if (size > 4096) { await reader.cancel(); throw Error(); }
      chunks.push(item.value);
    }
  } finally { reader.releaseLock(); }
  const raw = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { raw.set(chunk, offset); offset += chunk.byteLength; }
  const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(raw));
  if (!value || Object.keys(value).length !== 1 || typeof value.url !== 'string') throw Error();
  const target = new URL(value.url);
  if (target.protocol !== 'https:' || target.username || target.password || target.search || target.hash
      || !/^\/v2\/onboarding\/text-analysis-license-reissues\/[A-Za-z0-9_-]{43}$/.test(target.pathname)) throw Error();
  location.replace(target.href);
}).catch(unavailable);
