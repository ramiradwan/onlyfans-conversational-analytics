// Download-only R2 adapter. No signing keys, upload handlers, or release decisions.
const ORIGIN = 'https://updates.dipsy.fi';
const PREFIX = '/desktop/v1/';
const FEED = 'windows/x64/stable/ConversationAnalytics.appinstaller';
const ARTIFACT = '(?:ConversationAnalytics_[1-9][0-9]*(?:\\.[0-9]+){3}_x64\\.msix|(?:ConversationAnalytics|TextAnalysisEngine)_[1-9][0-9]*(?:\\.(?:0|[1-9][0-9]*)){3}_x64\\.msixbundle)';
const PACKAGE = new RegExp('^packages/[a-f0-9]{64}/' + ARTIFACT + '$');
const PROBE = new RegExp('^qualification/[a-f0-9]{32}/(?:bytes\\.bin|probe\\.msix|probe\\.appinstaller|packages/[a-f0-9]{64}/' + ARTIFACT + ')$');
const TYPES = {msix: 'application/msix', msixbundle: 'application/msixbundle', appinstaller: 'application/appinstaller', bin: 'application/octet-stream'};

function reply(request, status, text, extra = {}) {
  const body = new TextEncoder().encode(text);
  return new Response(request.method === 'HEAD' ? null : body, {status, headers: {
    'Content-Type': 'text/plain; charset=utf-8', 'Content-Length': String(body.length),
    'Cache-Control': 'no-store, no-transform', 'X-Content-Type-Options': 'nosniff',
    'X-Robots-Tag': 'noindex, nofollow', ...extra,
  }});
}

export function rangeFor(value, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2]) || size === 0) return null;
  const a = match[1] ? Number(match[1]) : null, b = match[2] ? Number(match[2]) : null;
  if ((a !== null && !Number.isSafeInteger(a)) || (b !== null && !Number.isSafeInteger(b))) return null;
  if (a === null) return b > 0 ? {offset: Math.max(0, size - b), length: Math.min(size, b)} : null;
  if (a >= size || (b !== null && b < a)) return null;
  return {offset: a, length: Math.min(b ?? (size - 1), size - 1) - a + 1};
}

function etagMatches(value, etag, weak = false) {
  return value.split(',').map(item => item.trim()).some(item => item === '*'
    || (weak ? item.replace(/^W\//, '') === etag : item === etag));
}

export async function serve(request, env) {
  const url = new URL(request.url);
  if (url.origin !== ORIGIN || !url.pathname.startsWith(PREFIX)) return reply(request, 404, 'Not found.');
  if (!['GET', 'HEAD'].includes(request.method)) return reply(request, 405, 'Method not allowed.', {Allow: 'GET, HEAD'});
  if (url.search || url.username || url.password || /[%\\\u0000-\u0020]/.test(url.pathname)) return reply(request, 400, 'Invalid path.');
  const key = url.pathname.slice(PREFIX.length);
  const probe = PROBE.test(key), immutable = PACKAGE.test(key);
  if (key !== FEED && !immutable && !probe) return reply(request, 404, 'Not found.');
  const bucket = probe ? env.QUALIFICATION : env.RELEASES;
  if (!bucket) return reply(request, 503, 'Temporarily unavailable.');
  const extension = key.slice(key.lastIndexOf('.') + 1);
  for (let attempt = 0; attempt < 2; attempt++) {
    const meta = await bucket.head(key);
    if (!meta) return reply(request, 404, 'Not found.');
    if (!Number.isSafeInteger(meta.size) || meta.size < 0) return reply(request, 503, 'Temporarily unavailable.');
    const etag = meta.httpEtag;
    const headers = new Headers({
      'Content-Type': TYPES[extension], 'Content-Length': String(meta.size), 'Content-Encoding': 'identity',
      'ETag': etag, 'Last-Modified': meta.uploaded.toUTCString(), 'Accept-Ranges': 'bytes',
      'Cache-Control': immutable ? 'public, max-age=31536000, immutable, no-transform' : 'no-store, no-transform',
      'X-Content-Type-Options': 'nosniff', 'X-Robots-Tag': 'noindex, nofollow', 'Referrer-Policy': 'no-referrer',
    });
    const ifMatch = request.headers.get('If-Match');
    if (ifMatch !== null && !etagMatches(ifMatch, etag)) return reply(request, 412, 'Precondition failed.');
    const noneMatch = request.headers.get('If-None-Match');
    if (noneMatch !== null && etagMatches(noneMatch, etag, true)) {
      headers.delete('Content-Length'); headers.delete('Content-Encoding');
      return new Response(null, {status: 304, headers});
    }
    let range = null;
    // HTTP Range applies only to GET; a HEAD must describe the complete object.
    if (request.method === 'GET' && request.headers.has('Range')) {
      const ifRange = request.headers.get('If-Range');
      const timestamp = ifRange === null ? NaN : Date.parse(ifRange);
      const unchanged = ifRange === null || ifRange === etag ||
        (Number.isFinite(timestamp) && Math.floor(meta.uploaded.getTime()/1000) <= Math.floor(timestamp/1000));
      if (unchanged) {
        range = rangeFor(request.headers.get('Range'), meta.size);
        if (range === null) return reply(request, 416, 'Range not satisfiable.', {'Content-Range': `bytes */${meta.size}`});
        headers.set('Content-Length', String(range.length));
        headers.set('Content-Range', `bytes ${range.offset}-${range.offset + range.length - 1}/${meta.size}`);
      }
    }
    if (request.method === 'HEAD') return new Response(null, {status: 200, headers});
    // Pin the read to the inspected object: promotion must not mix headers and bytes.
    const object = await bucket.get(key, {onlyIf: {etagMatches: meta.etag}, ...(range ? {range} : {})});
    if (!object || !('body' in object)) continue;
    return new Response(object.body, {status: range ? 206 : 200, headers, encodeBody: 'manual'});
  }
  return reply(request, 503, 'Temporarily unavailable.');
}

export default {
  async fetch(request, env) {
    try { return await serve(request, env); }
    catch { return reply(request, 503, 'Temporarily unavailable.'); }
  },
};
