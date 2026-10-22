import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { mkdtemp, open, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { BUCKET, BASE, ENDPOINT, FEED_URI, FEED_KEY, contextFromEnvironment, descriptorFields, versionSequence, loadRelease, publish } from './publisher.mjs';

const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const etag = bytes => '"' + createHash('md5').update(bytes).digest('hex') + '"';
function descriptor(version, sha) {
  return Buffer.from(`<?xml version="1.0" encoding="utf-8"?><AppInstaller xmlns="http://schemas.microsoft.com/appx/appinstaller/2018" Version="${version}" Uri="${FEED_URI}"><MainPackage Name="Dipsy.OnlyFansConversationalAnalytics" Publisher="CN=Dipsy, O=Dipsy, L=London, C=GB" Version="${version}" ProcessorArchitecture="x64" Uri="${BASE}packages/${sha}/ConversationAnalytics_${version}_x64.msix" /><UpdateSettings><OnLaunch HoursBetweenUpdateChecks="6" ShowPrompt="false" UpdateBlocksActivation="false" /><AutomaticBackgroundTask /></UpdateSettings></AppInstaller>`);
}
function environment() {
  return { GITHUB_ACTIONS:'true', GITHUB_EVENT_NAME:'workflow_dispatch', GITHUB_REPOSITORY:'ramiradwan/onlyfans-conversational-analytics',
    GITHUB_REF_TYPE:'tag', GITHUB_REF_NAME:'v0.7.5', GITHUB_SHA:'a'.repeat(40), PRODUCT_REVISION:'a'.repeat(40),
    GITHUB_RUN_ID:'42', GITHUB_RUN_ATTEMPT:'1', DESKTOP_RELEASE_TARGET:'msix-direct', DIRECT_PUBLICATION_REQUESTED:'true',
    DESKTOP_R2_BUCKET:BUCKET, DESKTOP_R2_ENDPOINT:ENDPOINT, DESKTOP_UPDATES_BASE_URL:BASE,
    MSIX_DIRECT_PUBLISHER:'CN=Dipsy, O=Dipsy, L=London, C=GB',
    MSIX_DIRECT_EXPECTED_FAMILY:'Dipsy.OnlyFansConversationalAnalytics_bvmm2ppd090g2', MSIX_DIRECT_APPINSTALLER_URI:FEED_URI };
}
function failure(code) { return Object.assign(new Error('synthetic request failure'), {$metadata:{httpStatusCode:code}}); }

async function intake(t, mutation) {
  // These synthetic receipts test byte/scope binding. The Windows admission
  // test separately proves that they cannot authorize an unsigned package.
  const root=await mkdtemp(join(tmpdir(),'desktop-intake-'));
  t.after(()=>rm(root,{recursive:true,force:true}));
  const context=contextFromEnvironment(environment()), version='1.0.9.0';
  const body=Buffer.from('deliberately unsigned synthetic bytes'), sha=digest(body);
  const xml=descriptor(version,sha), packageFile=`ConversationAnalytics_${version}_x64.msix`;
  const deliveryPath=join(root,'delivery.json');
  const deliveryBytes=await readFile(new URL('../../packaging/msix/direct-delivery.json',import.meta.url));
  await writeFile(deliveryPath,deliveryBytes);
  const record={schema:'ofca-desktop-artifact-release/v1',source_commit:context.sourceCommit,source_tree:'b'.repeat(40),
    target:'msix-direct',signature_scope:'trusted-timestamped',expected_family:environment().MSIX_DIRECT_EXPECTED_FAMILY,
    package_version:version,package_file:packageFile,package_sha256:sha,descriptor_sha256:digest(xml)};
  const verified={schema:'ofca-msix-release-verification/v1',mode:'DirectSigned',purpose:'production',
    distribution:'direct-appinstaller',signature:'trusted-timestamped',package_sha256:sha,
    identity:{Name:'Dipsy.OnlyFansConversationalAnalytics',Publisher:environment().MSIX_DIRECT_PUBLISHER,
      Version:version,Architecture:'x64',FamilyName:environment().MSIX_DIRECT_EXPECTED_FAMILY}};
  if(mutation==='store')record.target='msix-store';
  if(mutation==='source')record.source_commit='c'.repeat(40);
  if(mutation==='qualification')verified.purpose='qualification';
  if(mutation==='unsigned')verified.signature='unsigned-store-submission';
  if(mutation==='publisher')verified.identity.Publisher='CN=Other';
  if(mutation==='family')verified.identity.FamilyName='Other_family';
  const recordBytes=Buffer.from(JSON.stringify(record)), verificationBytes=Buffer.from(JSON.stringify(verified));
  const admission={schema:'ofca-direct-publication-admission/v1',source_commit:context.sourceCommit,source_tree:record.source_tree,
    artifact_record_sha256:digest(recordBytes),signature:'trusted-timestamped',verification_sha256:digest(verificationBytes),
    direct_delivery_sha256:digest(deliveryBytes),package_sha256:sha,descriptor_sha256:digest(xml)};
  if(mutation==='admission')admission.artifact_record_sha256='0'.repeat(64);
  const admissionPath=join(root,'admission.json'), verificationPath=join(root,'verification.json');
  await writeFile(join(root,'desktop.artifact-release.json'),recordBytes);
  await writeFile(verificationPath,verificationBytes);await writeFile(admissionPath,JSON.stringify(admission));
  await writeFile(join(root,packageFile),mutation==='package-bytes'?Buffer.from('changed bytes'):body);
  await writeFile(join(root,'ConversationAnalytics.appinstaller'),mutation==='descriptor-bytes'?Buffer.from('changed XML'):xml);
  return {root,admissionPath,verificationPath,deliveryPath,context,sha};
}

test('intake binds the independent verification and final package/descriptor bytes',async t=>{
  const f=await intake(t);const result=await loadRelease(f.root,f.admissionPath,f.verificationPath,f.deliveryPath,f.context);
  try {assert.equal(result.sha256,f.sha);assert.equal(result.key,`packages/${f.sha}/ConversationAnalytics_1.0.9.0_x64.msix`);}
  finally {await result.file.close();}
});
for(const mutation of ['store','source','qualification','unsigned','publisher','family','admission','package-bytes','descriptor-bytes']) {
  test(`intake refuses ${mutation} before a storage client is used`,async t=>{
    const f=await intake(t,mutation);
    await assert.rejects(loadRelease(f.root,f.admissionPath,f.verificationPath,f.deliveryPath,f.context));
  });
}

async function v2Intake(t, mutation) {
  const f=await intake(t);
  const recordPath=join(f.root,'desktop.artifact-release.json');
  const record=JSON.parse(await readFile(recordPath));
  const verification=JSON.parse(await readFile(f.verificationPath));
  const admission=JSON.parse(await readFile(f.admissionPath));
  const set={kind:'main_package',core:{file:record.package_file,sha256:record.package_sha256,
    size:(await readFile(join(f.root,record.package_file))).length}};
  Object.assign(record,{schema:'ofca-desktop-artifact-release/v2',artifact_set:set,
    application_version:'0.7.5',profile_sha256:'c'.repeat(64),inventory_sha256:'d'.repeat(64),
    build_sha256:'e'.repeat(64),verification_sha256:'f'.repeat(64),agent_file:'synthetic-chrome.zip',
    agent_sha256:'1'.repeat(64),resolved_direct_inputs_sha256:'2'.repeat(64)});
  delete record.package_file;delete record.package_sha256;
  Object.assign(verification,{schema:'ofca-msix-release-verification/v2',artifact_set:structuredClone(set),
    file_count:1,profile_sha256:record.profile_sha256,inventory_sha256:record.inventory_sha256});
  verification.identity.FullName='Dipsy.OnlyFansConversationalAnalytics_1.0.9.0_x64__bvmm2ppd090g2';
  Object.assign(admission,{schema:'ofca-direct-publication-admission/v2',artifact_set:structuredClone(set),identity:verification.identity});
  delete admission.package_sha256;
  if(mutation==='related')record.artifact_set={kind:'related_bundle'};
  if(mutation==='size')record.artifact_set.core.size++;
  if(mutation==='alias')record.package_file='legacy.msix';
  if(mutation==='extra-verification')verification.fallback=true;
  let verificationBytes=Buffer.from(JSON.stringify(verification));
  if(mutation==='duplicate-verification')verificationBytes=Buffer.from(JSON.stringify(verification).replace('"file_count":1','"file_count":0,"file_count":1'));
  admission.verification_sha256=digest(verificationBytes);record.verification_sha256=admission.verification_sha256;
  let recordBytes=Buffer.from(JSON.stringify(record));
  if(mutation==='duplicate-record')recordBytes=Buffer.from(JSON.stringify(record).replace('"package_version":','"package_version":"1.0.8.0","package_version":'));
  if(mutation==='nonfinite')recordBytes=Buffer.from(JSON.stringify(record).replace('"size":'+set.core.size,'"size":1e400'));
  admission.artifact_record_sha256=digest(recordBytes);
  await writeFile(recordPath,recordBytes);await writeFile(f.verificationPath,verificationBytes);
  await writeFile(f.admissionPath,JSON.stringify(admission));return f;
}

test('v2 MainPackage intake binds the closed signed artifact set',async t=>{
  const f=await v2Intake(t),result=await loadRelease(f.root,f.admissionPath,f.verificationPath,f.deliveryPath,f.context);
  try{assert.equal(result.sha256,f.sha);}finally{await result.file.close();}
});
for(const mutation of ['related','size','alias','extra-verification','duplicate-verification','duplicate-record','nonfinite']) {
  test('v2 intake refuses '+mutation+' before storage access',async t=>{
    const f=await v2Intake(t,mutation);
    await assert.rejects(loadRelease(f.root,f.admissionPath,f.verificationPath,f.deliveryPath,f.context));
  });
}

async function fixture(t, options = {}) {
  const root = await mkdtemp(join(tmpdir(), 'desktop-publication-'));
  const bytes = Buffer.from('synthetic nonshipping package data '.repeat(100));
  const version = '1.0.9.0', sha = digest(bytes);
  const path = join(root, 'package.bin'); await writeFile(path, bytes);
  const file = await open(path, 'r');
  t.after(async () => { await file.close(); await rm(root,{recursive:true,force:true}); });
  const release = { file, size:bytes.length, sha256:sha, prefix:bytes.subarray(0,1024), sequence:9,
    descriptor:descriptor(version,sha), key:`packages/${sha}/ConversationAnalytics_${version}_x64.msix` };
  const state = new Map(), calls = [], events = [];
  function object(body, metadata = {}) { return {body,metadata,etag:etag(body)}; }
  if (options.previous) state.set(FEED_KEY,object(options.previous));
  if (options.existingPackage) state.set(release.key,object(options.existingPackage,{'release-sha256':sha}));
  const client = { async send(command) {
    const {Bucket,Key,Body,IfMatch,IfNoneMatch,Metadata}=command.input;
    assert.equal(Bucket,BUCKET); assert.ok(Key===FEED_KEY || Key===release.key);
    const name=command.constructor.name; calls.push({name,key:Key,ifMatch:IfMatch,ifNoneMatch:IfNoneMatch});
    await options.beforeSend?.({name,key:Key,state,object});
    if (options.failRequest?.({name,key:Key})) throw failure(503);
    const current=state.get(Key);
    if (name==='PutObjectCommand') {
      if ((IfNoneMatch==='*' && current) || (IfMatch && current?.etag!==IfMatch)) throw failure(412);
      assert.ok(IfNoneMatch==='*' || typeof IfMatch==='string', 'every write must be conditional');
      const chunks=[];
      if (Buffer.isBuffer(Body)) chunks.push(Body);
      else for await (const chunk of Body) chunks.push(chunk);
      const body=Buffer.concat(chunks); assert.equal(body.length,command.input.ContentLength);
      state.set(Key,object(body,Metadata)); return {$metadata:{httpStatusCode:200}};
    }
    if (name==='GetObjectCommand' || name==='HeadObjectCommand') {
      if (!current) throw failure(404);
      return {Body:name==='GetObjectCommand'?Readable.from([current.body]):undefined,
        ContentLength:current.body.length,Metadata:current.metadata,ETag:current.etag};
    }
    assert.fail('No list, delete, routing or bucket administration operation is permitted');
  }};
  const fetcher=async (uri,request) => {
    assert.equal(request.redirect,'error'); assert.equal(request.headers['Accept-Encoding'],'identity');
    const key=uri.slice(BASE.length), current=state.get(key); assert.ok(current);
    calls.push({name:request.method==='HEAD'?'public_head':'public_get',key});
    await options.beforeFetch?.({key,state,object});
    const range=request.headers.Range;
    const size=range?Math.min(1024,current.body.length):current.body.length;
    let body=request.method==='HEAD'?null:current.body.subarray(0,size);
    const headers={'Content-Type':key===FEED_KEY?'application/appinstaller':'application/msix','Content-Length':String(size),
      'Content-Encoding':'identity','Accept-Ranges':'bytes','Cache-Control':key===FEED_KEY?'no-store, no-transform':'public, max-age=31536000, immutable, no-transform'};
    if (range) headers['Content-Range']=`bytes 0-${size-1}/${current.body.length}`;
    let status=range?206:200, url=uri;
    if (key!==FEED_KEY && request.method!=='HEAD') {
      if (options.corruptPackage && !range) body=Buffer.alloc(size,0);
      if (options.corruptRange && range) body=Buffer.alloc(size,0);
    }
    const altered=options.alterResponse?.({key,request,headers,body,status,url}) ?? {};
    const response=new Response(altered.body??body,{status:altered.status??status,headers:altered.headers??headers});
    Object.defineProperty(response,'url',{value:altered.url??url}); return response;
  };
  const event=async receipt=>{events.push(structuredClone(receipt));await options.event?.(receipt);};
  return {release,state,calls,events,client,fetcher,event,context:contextFromEnvironment(environment()),bytes};
}

test('fresh publication verifies package before conditional descriptor promotion',async t=>{
  const f=await fixture(t); const result=await publish(f.client,f.release,f.context,f.fetcher,f.event);
  assert.equal(result.success,true);
  assert.deepEqual(result.stages,['package_uploaded','served_package_verified','descriptor_promoted','served_descriptor_verified']);
  const feed=f.calls.findIndex(c=>c.name==='PutObjectCommand' && c.key===FEED_KEY);
  assert.ok(feed>f.calls.findIndex(c=>c.name==='public_get' && c.key===f.release.key));
  assert.equal(f.calls[feed].ifNoneMatch,'*'); assert.equal(f.events[0].stages.length,0);
});
test('existing exact package requires a real read/hash and is reused without overwrite',async t=>{
  const f=await fixture(t,{existingPackage:Buffer.from('synthetic nonshipping package data '.repeat(100))});
  const result=await publish(f.client,f.release,f.context,f.fetcher,f.event);
  assert.equal(result.stages[0],'identical_package_reused');
  assert.ok(f.calls.some(c=>c.name==='GetObjectCommand' && c.key===f.release.key));
});
test('matching metadata cannot substitute for existing immutable bytes',async t=>{
  const bad=Buffer.from('synthetic nonshipping package data '.repeat(100));bad[0]^=1;
  const f=await fixture(t,{existingPackage:bad});
  await assert.rejects(publish(f.client,f.release,f.context,f.fetcher,f.event),/immutable_object_conflict/);
  assert.ok(!f.calls.some(c=>c.name==='PutObjectCommand' && c.key===FEED_KEY));
});
for(const [version,sha,reason] of [['1.0.10.0','1'.repeat(64),'descriptor_downgrade_refused'],['1.0.9.0','1'.repeat(64),'same_version_replacement_refused']]) {
  test(`existing ${version} refuses ${reason} before any write`,async t=>{
    const f=await fixture(t,{previous:descriptor(version,sha)});
    await assert.rejects(publish(f.client,f.release,f.context,f.fetcher,f.event),new RegExp(reason));
    assert.ok(!f.calls.some(c=>c.name==='PutObjectCommand'));
  });
}
test('concurrent descriptor promotion cannot overwrite a newer release',async t=>{
  const previous=descriptor('1.0.8.0','8'.repeat(64)), newer=descriptor('1.0.10.0','0'.repeat(64));
  const f=await fixture(t,{previous,beforeFetch:({key,state,object})=>{if(key!==FEED_KEY)state.set(FEED_KEY,object(newer));}});
  await assert.rejects(publish(f.client,f.release,f.context,f.fetcher,f.event));
  assert.deepEqual(f.state.get(FEED_KEY).body,newer);
  const promotion=f.calls.find(c=>c.name==='PutObjectCommand' && c.key===FEED_KEY);
  assert.equal(promotion.ifMatch,etag(previous));
});
test('exact same descriptor rerun is idempotent and never rewrites the feed',async t=>{
  const f=await fixture(t);f.state.set(FEED_KEY,{body:f.release.descriptor,etag:etag(f.release.descriptor)});
  assert.equal((await publish(f.client,f.release,f.context,f.fetcher,f.event)).success,true);
  assert.ok(!f.calls.some(c=>c.name==='PutObjectCommand' && c.key===FEED_KEY));
});
for(const mutation of ['hash','length','type','compression','ranges','transform','redirect','range-bytes','range-header']) {
  test(`served ${mutation} failure prevents descriptor promotion`,async t=>{
    const f=await fixture(t,{corruptPackage:mutation==='hash',corruptRange:mutation==='range-bytes',alterResponse:({key,headers,url,request})=>{
      if(key===FEED_KEY)return {};
      if(mutation==='length')headers['Content-Length']='1';
      if(mutation==='type')headers['Content-Type']='text/plain';
      if(mutation==='compression')headers['Content-Encoding']='gzip';
      if(mutation==='ranges')delete headers['Accept-Ranges'];
      if(mutation==='transform')headers['Cache-Control']='public';
      if(mutation==='range-header' && request.headers.Range)headers['Content-Range']='bytes 0-1/2';
      return mutation==='redirect'?{url:url+'/'}:{};
    }});
    await assert.rejects(publish(f.client,f.release,f.context,f.fetcher,f.event));
    assert.ok(!f.calls.some(c=>c.name==='PutObjectCommand' && c.key===FEED_KEY));
    assert.equal(f.events.at(-1).success,false);
  });
}
test('durable intent failure prevents either write',async t=>{
  const f=await fixture(t,{event:()=>{throw Error('synthetic evidence write failure');}});
  await assert.rejects(publish(f.client,f.release,f.context,f.fetcher,f.event));
  assert.ok(!f.calls.some(c=>c.name==='PutObjectCommand'));
});
test('stored descriptor access denial is not a first release',async t=>{
  const f=await fixture(t,{failRequest:({name,key})=>name==='GetObjectCommand' && key===FEED_KEY});
  await assert.rejects(publish(f.client,f.release,f.context,f.fetcher,f.event));
  assert.ok(!f.calls.some(c=>c.name==='PutObjectCommand'));
});
test('source, explicit opt-in, direct channel and six approved inputs are all required',()=>{
  for(const field of ['PRODUCT_REVISION','GITHUB_REF_NAME','GITHUB_REPOSITORY','GITHUB_EVENT_NAME','DESKTOP_RELEASE_TARGET',
    'DIRECT_PUBLICATION_REQUESTED','DESKTOP_R2_BUCKET','DESKTOP_R2_ENDPOINT','DESKTOP_UPDATES_BASE_URL','MSIX_DIRECT_PUBLISHER',
    'MSIX_DIRECT_EXPECTED_FAMILY','MSIX_DIRECT_APPINSTALLER_URI']) {
    assert.throws(()=>contextFromEnvironment({...environment(),[field]:'unapproved'}));
  }
  assert.throws(()=>contextFromEnvironment({...environment(),GITHUB_REF_NAME:'vcredential-check-20261014-8dea188d'}));
});
test('closed descriptor policy refuses extra update authority, redirects, Store identity and mismatched versions',()=>{
  const xml=descriptor('1.0.9.0','f'.repeat(64)).toString();
  assert.equal(descriptorFields(Buffer.from(xml)).sequence,9);
  for(const changed of [xml.replace('ShowPrompt="false"','ShowPrompt="true"'),xml.replace('</UpdateSettings>','<ForceUpdateFromAnyVersion>true</ForceUpdateFromAnyVersion></UpdateSettings>'),
    xml.replace('Dipsy.OnlyFansConversationalAnalytics','Dipsy.ConversationAnalytics'),xml.replace(FEED_URI,FEED_URI+'/'),
    xml.replace('Version="1.0.9.0" Processor','Version="1.0.8.0" Processor'), '<!DOCTYPE x [<!ENTITY evil "test">]>'+xml]) {
    assert.throws(()=>descriptorFields(Buffer.from(changed)));
  }
  assert.ok(versionSequence('1.1.0.0')>versionSequence('1.0.65535.0'));
  for(const value of ['1.0.0.0','1.0.65536.0','1.01.9.0','0.7.5','1.0.9.1'])assert.throws(()=>versionSequence(value));
});
test('real pinned SDK serializes both atomic write preconditions',async()=>{
  const observed=[];
  const client=new S3Client({region:'auto',endpoint:ENDPOINT,maxAttempts:1,
    requestChecksumCalculation:'WHEN_REQUIRED',credentials:{accessKeyId:'synthetic',secretAccessKey:'synthetic'},
    requestHandler:{handle:async request=>{observed.push(request.headers);return {response:{statusCode:200,headers:{},body:Readable.from([])}};}}});
  try {
    await client.send(new PutObjectCommand({Bucket:BUCKET,Key:FEED_KEY,Body:Buffer.from('synthetic'),IfNoneMatch:'*'}));
    await client.send(new PutObjectCommand({Bucket:BUCKET,Key:FEED_KEY,Body:Buffer.from('synthetic'),IfMatch:'"'+'a'.repeat(32)+'"'}));
    assert.equal(observed[0]['if-none-match'],'*');assert.equal(observed[1]['if-match'],'"'+'a'.repeat(32)+'"');
  } finally {client.destroy();}
});
