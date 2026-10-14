// Platform-side fixture, with actual document-bound fetches observed by Chrome.
// No Chrome/document/capture/signing host is replaced. Independent arithmetic
// validates candidate and control requests at the mocked platform boundary.
import { createHash } from 'node:crypto';
import { fixtureRule } from './parameter-publication-producer.mjs';

export function signature(rule, time, pathname, identity) {
  const hash = createHash('sha1').update([rule.static_param, time, pathname, identity].join('\n')).digest('hex');
  const sum = Math.abs(rule.format.checksum_indexes.reduce((value, index) => value + hash.charCodeAt(index), rule.format.checksum_constant));
  return [rule.format.prefix, hash, sum.toString(16), rule.format.suffix].join(':');
}

export function platformDocument(revision) {
  // Native polling belongs to the fixture platform. A failed dependency drops
  // captures; its next fresh poll supplies another real native observation.
  // This does not navigate/reload a customer's document or fabricate a getter.
  return `<!doctype html><title>Public synthetic platform fixture</title><script>
    const rule=${JSON.stringify(fixtureRule(revision))};
    async function nativeIdentity() {
      const time=String(Date.now()), pathname='/api2/v2/users/me';
      const bytes=await crypto.subtle.digest('SHA-1',new TextEncoder().encode([rule.static_param,time,pathname,'0'].join('\\n')));
      const hash=[...new Uint8Array(bytes)].map(x=>x.toString(16).padStart(2,'0')).join('');
      const checksum=Math.abs(rule.format.checksum_indexes.reduce((sum,index)=>sum+hash.charCodeAt(index),rule.format.checksum_constant));
      await fetch(pathname,{headers:{accept:'application/json','app-token':'synthetic-app-token','x-bc':'synthetic-browser-context',
        'x-of-rev':rule.source_revision,time,sign:[rule.format.prefix,hash,checksum.toString(16),rule.format.suffix].join(':')}});
    }
    setInterval(()=>nativeIdentity().catch(()=>{}),500); nativeIdentity().catch(()=>{});
  </script>`;
}

export function platformBoundary() {
  let revision = 'frontend-A', marker = 'backend-B', account = '12345';
  const counters = { native: 0, candidate: 0, control: 0, reads: 0, revisions: {}, cursors: [] };
  const reply = (url, headers) => {
      if (!url.pathname.startsWith('/api2/v2/')) return { status: 200,
        contentType: 'text/html', body: platformDocument(revision) };
      const native = !Object.hasOwn(headers, 'user-id');
      const valid = headers['x-of-rev'] === revision
        && signature(fixtureRule(revision), headers.time, url.pathname + url.search, headers['user-id'] ?? '0') === headers.sign;
      if (native) counters.native++;
      else {
        counters.revisions[headers['x-of-rev']] = (counters.revisions[headers['x-of-rev']] ?? 0) + 1;
        if (!valid) counters.control++;
        else if (url.pathname.endsWith('/users/me')) counters.candidate++;
        else counters.reads++;
      }
      const responseHeaders = marker === null ? {} : { 'x-of-rev': marker };
      if (!valid) return { status: 400, contentType: 'application/json', headers: responseHeaders,
        body: JSON.stringify({ error: 'synthetic-signature-rejection' }) };
      let body;
      if (url.pathname.endsWith('/users/me')) body = { id: account, name: 'Public fixture', username: 'public_fixture' };
      else if (url.pathname === '/api2/v2/chats') {
        const offset = Number(url.searchParams.get('offset') ?? 0); counters.cursors.push(offset);
        body = { list: Array.from({ length: 100 }, (_, i) => ({ id: String(offset + i + 1000),
          withUser: { id: String(offset + i + 1000), name: 'Synthetic fan' }, updatedAt: new Date().toISOString() })),
        hasMore: true, nextOffset: offset + 100 };
      } else body = { list: [], hasMore: false };
      return { status: 200, contentType: 'application/json', headers: responseHeaders, body: JSON.stringify(body) };
  };
  return { counters, setRevision: value => { revision = value; }, setMarker: value => { marker = value; },
    setAccount: value => { account = value; },
    handle(request, response) {
      const result = reply(new URL(request.url, 'https://onlyfans.com'), request.headers);
      response.writeHead(result.status, { 'Content-Type': result.contentType, 'Cache-Control': 'no-store', ...result.headers });
      response.end(result.body);
    },
  };
}
