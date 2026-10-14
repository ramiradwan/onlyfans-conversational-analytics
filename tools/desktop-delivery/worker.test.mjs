import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';
import {rangeFor, serve} from './worker.mjs';

const base = 'https://updates.dipsy.fi/desktop/v1/';
const feed = 'windows/x64/stable/ConversationAnalytics.appinstaller';
const packageKey = `packages/${'a'.repeat(64)}/ConversationAnalytics_1.0.1.0_x64.msix`;
const probeKey = `qualification/${'b'.repeat(32)}/bytes.bin`;
const bytes = Uint8Array.from({length:65536}, (_, i) => i % 251);

test('strict bounded range parser', () => {
 for (const bad of ['bytes=-','bytes=1-0','bytes=65536-','bytes=-0','bytes=0-1,3-4','bytes=9007199254740992-','other=0-3']) assert.equal(rangeFor(bad, bytes.length), null);
 assert.deepEqual(rangeFor('bytes=10-19',bytes.length),{offset:10,length:10});
 assert.deepEqual(rangeFor('bytes=-10',bytes.length),{offset:65526,length:10});
 assert.deepEqual(rangeFor('bytes=65530-',bytes.length),{offset:65530,length:6});
 assert.equal(rangeFor('bytes=0-0',0),null);
});

test('concurrent replacement is bounded and cannot mix metadata with bytes', async () => {
 let tries=0;
 const meta={size:10,httpEtag:'"one"',etag:'one',uploaded:new Date('2026-10-14T00:00:00Z')};
 const response=await serve(new Request(base+feed),{RELEASES:{head:async()=>meta,get:async()=>{tries++;return meta;}}});
 assert.equal(response.status,503);assert.equal(tries,2);
});

test('actual workerd/R2 request and storage boundaries', async t => {
 const mf=new Miniflare(convertV4MiniflareOptions({workers:[{name:'downloads',modules:true,script:await readFile(new URL('./worker.mjs',import.meta.url),'utf8'),
   compatibilityDate:'2026-10-13',r2Buckets:['RELEASES','QUALIFICATION']}]}));
 try {
  const releases=await mf.getR2Bucket('RELEASES'),qual=await mf.getR2Bucket('QUALIFICATION');
  await releases.put(packageKey,bytes); await releases.put(feed,'<AppInstaller/>');await qual.put(probeKey,bytes);
  const request=(key,options={})=>mf.dispatchFetch(base+key,options);
  let etag;
  await t.test('GET streams exact immutable package bytes and MIME',async()=>{
   const r=await request(packageKey);assert.equal(r.status,200);etag=r.headers.get('etag');
   assert.equal(r.headers.get('content-type'),'application/msix');assert.equal(r.headers.get('content-length'),String(bytes.length));
   assert.match(r.headers.get('cache-control'),/immutable/);assert.match(r.headers.get('cache-control'),/no-transform/);
   assert.deepEqual(new Uint8Array(await r.arrayBuffer()),bytes);
  });
  await t.test('HEAD ignores Range and has the full byte length and no body',async()=>{
   const r=await request(packageKey,{method:'HEAD',headers:{Range:'bytes=0-9'}});assert.equal(r.status,200);
   assert.equal(r.headers.get('content-length'),String(bytes.length));assert.equal(r.headers.get('content-range'),null);assert.equal((await r.arrayBuffer()).byteLength,0);
  });
  for(const [header,start,end] of [['bytes=13-31',13,32],['bytes=65530-',65530,65536],['bytes=-5',65531,65536]]) await t.test('range '+header,async()=>{
   const r=await request(packageKey,{headers:{Range:header}});assert.equal(r.status,206);
   assert.equal(r.headers.get('content-range'),`bytes ${start}-${end-1}/${bytes.length}`);
   assert.deepEqual(new Uint8Array(await r.arrayBuffer()),bytes.slice(start,end));
  });
  await t.test('unsatisfiable and multipart ranges refuse',async()=>{
   for(const range of ['bytes=65536-','bytes=0-1,3-4']){const r=await request(packageKey,{headers:{Range:range}});assert.equal(r.status,416);assert.equal(r.headers.get('content-range'),`bytes */${bytes.length}`);}
  });
  await t.test('matching If-Range resumes; changed identity returns whole object',async()=>{
   const a=await request(packageKey,{headers:{Range:'bytes=0-9','If-Range':etag}});assert.equal(a.status,206);await a.arrayBuffer();
   const b=await request(packageKey,{headers:{Range:'bytes=0-9','If-Range':'"other"'}});assert.equal(b.status,200);assert.equal((await b.arrayBuffer()).byteLength,bytes.length);
  });
  await t.test('conditional reads',async()=>{
   assert.equal((await request(packageKey,{headers:{'If-None-Match':etag}})).status,304);
   assert.equal((await request(packageKey,{headers:{'If-None-Match':'W/'+etag}})).status,304);
   assert.equal((await request(packageKey,{headers:{'If-Match':'"other"'}})).status,412);
  });
  await t.test('changing App Installer feed is never immutable-cached',async()=>{
   const r=await request(feed);assert.equal(r.headers.get('content-type'),'application/appinstaller');assert.equal(r.headers.get('cache-control'),'no-store, no-transform');
   assert.equal(await r.text(),'<AppInstaller/>');
  });
  await t.test('qualification bytes use only the separate bucket',async()=>{
   assert.equal((await request(probeKey)).status,200);await qual.delete(probeKey);await releases.put(probeKey,bytes);
   assert.equal((await request(probeKey)).status,404);
  });
  for(const method of ['PUT','POST','DELETE','PATCH','OPTIONS']) await t.test(method+' cannot mutate storage',async()=>{
   const r=await request(packageKey,{method});assert.equal(r.status,405);assert.equal(r.headers.get('allow'),'GET, HEAD');
  });
  await t.test('missing paths and unsafe inputs fail without fallback or caching',async()=>{
   for(const key of ['not-found.msix','../signing-parameters/v1/catalog.jws',packageKey+'?download=1','packages/%2fsecret.msix']){
    const r=await request(key);assert.ok([400,404].includes(r.status));assert.match(r.headers.get('cache-control'),/no-store/);
   }
   assert.equal((await mf.dispatchFetch('https://other.example/desktop/v1/'+feed)).status,404);
   assert.equal((await mf.dispatchFetch('http://updates.dipsy.fi/desktop/v1/'+feed)).status,404);
  });
  await t.test('missing stable pointer is an uncached refusal, not fake release metadata',async()=>{
   await releases.delete(feed);const r=await request(feed);assert.equal(r.status,404);assert.match(r.headers.get('cache-control'),/no-store/);
  });
 } finally {await mf.dispose();}
});
