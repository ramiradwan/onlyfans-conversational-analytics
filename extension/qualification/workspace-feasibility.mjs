// A real Chromium MV3 package around the production workspace coordinator.
// Brain/hosted pages are explicit fixtures. This does not qualify their real
// authentication, installers, pairing, or the Windows application protocol.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';

const args = process.argv.slice(2);
const option = (name) => args[args.indexOf(name) + 1];
const here = path.dirname(fileURLToPath(import.meta.url));
const moduleDirectory = args.includes('--modules-dir') ? path.resolve(option('--modules-dir')) : path.join(here, '../node_modules');
const require = createRequire(path.join(moduleDirectory, 'workspace-feasibility.cjs'));
const { chromium } = require('playwright');
const { build } = require('esbuild');
const root = await mkdtemp(path.join(tmpdir(), 'ofca-workspace-'));
const packaged = path.join(root, 'extension');
const profile = path.join(root, 'profile');
const journey = 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fed';
const scope = { scope_id: 'b88c2d88-dbbf-4fdb-8743-9f92cbe46fea', disclosure_bundle_id: 'a'.repeat(64) };
const checks = [];
const report = { schema: 'ofca-workspace-feasibility/v1', checks, browser: null,
  generated_at: new Date().toISOString(),
  scope: 'Real bundled MV3 coordinator and browser lifecycle; fixture hosted and Brain services; no production sign-in, app installer, pairing, or live OnlyFans account.',
  evidence: {}, limitations: [] };
let context, server, cdp;
let brainEpoch = 1, brainStarts = 0, brainRequests = 0;
const streams = new Set();
let localPort;
let extensionId;
const pageScript = () => `
globalThis.initialRuntimeAvailable = typeof chrome?.runtime?.connect === 'function';
globalThis.discover = () => typeof chrome?.runtime?.connect === 'function';
globalThis.request = (type, payload = {}) => chrome.runtime.sendMessage(${JSON.stringify(extensionId)}, {type,...payload});
globalThis.documentNonce = crypto.randomUUID();
if(location.pathname === '/provisioning/') {
  globalThis.brainEvents=[];
  const source = new EventSource('/events');
  source.onmessage = ({data}) => { brainEvents.push(JSON.parse(data)); document.querySelector('output').textContent=data; };
}
document.querySelector('#next').onclick=()=>request('navigate',{route:location.pathname === '/setup' ? 'provisioning' : 'extension'});
`;
const pageHtml = '<!doctype html><title>Workspace fixture</title><button id="next">Continue</button><output></output><script src="/fixture.js"></script>';
async function startBrain(port = 0) {
  server = createServer((request, response) => {
    brainRequests++;
    if (request.url === '/events') {
      response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
      response.write(`data: ${JSON.stringify({ epoch: brainEpoch, state: 'ready' })}\n\n`);
      streams.add(response); request.on('close', () => streams.delete(response));
    } else {
      response.writeHead(200, { 'Content-Type': request.url === '/fixture.js' ? 'text/javascript' : 'text/html' });
      response.end(request.url === '/fixture.js' ? pageScript() : pageHtml);
    }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
  brainStarts++;
  localPort = server.address().port;
}
async function stopBrain() {
  for (const response of streams) response.end();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}
async function check(name, run) {
  await run(); checks.push({ name, passed: true }); console.log(`PASS ${name}`);
}
try {
  await startBrain();
  await mkdir(packaged);
  const routes = { hosted: 'https://onboarding.example.test/setup',
    provisioning: `http://bridge.localhost:${localPort}/provisioning/`, bridge: `http://bridge.localhost:${localPort}/` };
  await build({ stdin: { contents: `
import {createOnboardingWorkspace} from '../runtime/onboarding-workspace.mjs';
const routes={...${JSON.stringify(routes)},extension:chrome.runtime.getURL('setup.html')};
const scope=${JSON.stringify(scope)};
const workspace=createOnboardingWorkspace({chromeApi:chrome,routes});
globalThis.workspace=workspace;
globalThis.ready=true;
globalThis.workerNonce=crypto.randomUUID();
const listen=(message,sender,reply)=>{
  let operation;
  if(message.type==='read') operation=workspace.read();
  if(message.type==='navigate') operation=workspace.navigate(sender,{route:message.route});
  if(message.type==='draft') operation=workspace.saveDraft(sender,{scope_id:message.scope_id,draft:message.draft});
  if(message.type==='open') operation=workspace.open({journey_id:${JSON.stringify(journey)},route:'extension',explicit:true,draft_scope:scope});
  if(!operation)return false;
  operation.then(value=>reply({ok:true,value}),error=>reply({ok:false,error:error.message}));return true;
};
chrome.runtime.onMessage.addListener(listen);
chrome.runtime.onMessageExternal.addListener(listen);
chrome.action.onClicked.addListener(()=>workspace.open({journey_id:${JSON.stringify(journey)},route:'extension',explicit:true,draft_scope:scope}));
chrome.runtime.onInstalled.addListener(()=>{globalThis.installObserved=true;workspace.resumeExisting({draft_scope:scope,route:'extension'}).then(value=>globalThis.installResult=value,error=>globalThis.installError=error.message)});
`, resolveDir: here }, bundle: true, format: 'esm', target: 'chrome132', outfile: path.join(packaged, 'background.js') });
  await writeFile(path.join(packaged, 'manifest.json'), JSON.stringify({ manifest_version: 3,
    name: 'Onboarding workspace feasibility', version: '1.0.0', permissions: ['storage'],
    host_permissions: ['http://bridge.localhost/*', 'https://onboarding.example.test/*'],
    externally_connectable: { matches: ['http://bridge.localhost/*', 'https://onboarding.example.test/*'] },
    background: { service_worker: 'background.js', type: 'module' }, action: { default_title: 'Open setup' },
    content_security_policy: { extension_pages: "script-src 'self'; object-src 'self'" },
  }, null, 2));
  await writeFile(path.join(packaged, 'setup.html'), '<!doctype html><title>Extension workspace fixture</title><label><input id="terms" type="checkbox">Terms</label><button id="next">Continue</button><output></output><script src="setup.js"></script>');
  await writeFile(path.join(packaged, 'setup.js'), `
globalThis.request=(type,payload={})=>chrome.runtime.sendMessage({type,...payload});
globalThis.documentNonce=crypto.randomUUID();
request('read').then(({value})=>{document.querySelector('#terms').checked=value.draft.terms_checked;globalThis.loaded=value});
document.querySelector('#terms').onchange=()=>request('draft',{scope_id:${JSON.stringify(scope.scope_id)},draft:{terms_checked:document.querySelector('#terms').checked,risk_checked:false,full_checked:false}}).then(value=>globalThis.saved=value);
document.querySelector('#next').onclick=()=>request('navigate',{route:'hosted'});
`);
  context = await chromium.launchPersistentContext(profile, { channel: 'chromium', headless: true,
    ignoreDefaultArgs: ['--disable-extensions'],
    args: ['--enable-unsafe-extension-debugging', '--no-proxy-server', '--host-resolver-rules=MAP bridge.localhost 127.0.0.1'],
    ...(args.includes('--browser-executable') ? { executablePath: option('--browser-executable') } : {}) });
  cdp = await context.browser().newBrowserCDPSession();
  context.on('console', (message) => { if (message.type() === 'error') console.error(message.text()); });
  report.browser = context.browser().version();
  await context.route('https://onboarding.example.test/**', async (route) => route.fulfill({ status: 200,
    contentType: route.request().url().endsWith('/fixture.js') ? 'text/javascript' : 'text/html',
    body: route.request().url().endsWith('/fixture.js') ? pageScript() : pageHtml }));
  // Synthetic OnlyFans URL: browser never contacts the real service.
  await context.route('https://onlyfans.com/**', async (route) => route.fulfill({ status: 200, contentType: 'text/html',
    body: '<!doctype html><title>Unrelated creator tab fixture</title><textarea>Unsaved draft</textarea><script>globalThis.nonce=crypto.randomUUID()</script>' }));
  let page = context.pages()[0] ?? await context.newPage();
  await page.goto(`${routes.bridge}#journey=${journey}`);
  const initialNonce = await page.evaluate(() => documentNonce);
  report.evidence.preinstall_runtime_available = await page.evaluate(() => initialRuntimeAvailable);
  assert.equal(report.evidence.preinstall_runtime_available, false);
  const userTab = await context.newPage(); await userTab.goto('https://onlyfans.com/my/chats');
  const userNonce = await userTab.evaluate(() => nonce);
  const userTarget = (await cdp.send('Target.getTargets', { filter: [{ type: 'tab', exclude: false }] })).targetInfos
    .find((target) => target.type === 'tab' && target.url === userTab.url())?.targetId;
  assert.ok(userTarget, 'DevTools must expose the browser tab target for a real toolbar action');
  const pageSession = await context.newCDPSession(page);
  const initialTarget = (await pageSession.send('Target.getTargetInfo')).targetInfo.targetId;
  let userNavigations = 0; userTab.on('framenavigated', (frame) => { if (frame === userTab.mainFrame()) userNavigations++; });
  const { id } = await cdp.send('Extensions.loadUnpacked', { path: packaged }); extensionId = id;
  let worker = context.serviceWorkers().find((item) => item.url().startsWith(`chrome-extension://${extensionId}/`))
    ?? await context.waitForEvent('serviceworker', { timeout: 10000 });
  const extensionUrl = `chrome-extension://${extensionId}/setup.html#journey=${journey}`;
  await check('install into an existing journey adopts and resumes the same tab without focusing it', async () => {
    await page.waitForURL(extensionUrl); await page.waitForFunction(() => globalThis.loaded);
    assert.equal((await pageSession.send('Target.getTargetInfo')).targetInfo.targetId, initialTarget);
    assert.notEqual(await page.evaluate(() => documentNonce), initialNonce);
    assert.equal(context.pages().length, 2);
    assert.equal(await userTab.evaluate(() => document.hasFocus()), true);
  });
  assert.ok(worker);
  await check('checkbox draft is durable and has no granted authority', async () => {
    await page.locator('#terms').check(); await page.waitForFunction(() => globalThis.saved);
    assert.deepEqual(await page.evaluate(() => saved.ok ? {ok:true} : saved), {ok:true});
    await page.reload(); await page.waitForFunction(() => globalThis.loaded);
    assert.equal(await page.locator('#terms').isChecked(), true);
    assert.deepEqual(Object.keys(await worker.evaluate(() => workspace.read())).sort(), ['draft', 'draft_scope', 'journey_id', 'route', 'version']);
  });
  await check('extension, hosted, provisioning and Bridge navigation preserve the same browser tab', async () => {
    await page.locator('#next').click(); await page.waitForURL(`${routes.hosted}#journey=${journey}`);
    assert.equal(await page.evaluate(() => discover()), true);
    await page.locator('#next').click(); await page.waitForURL(`${routes.provisioning}#journey=${journey}`);
    await page.waitForFunction(() => globalThis.brainEvents?.length === 1);
    assert.equal((await pageSession.send('Target.getTargetInfo')).targetInfo.targetId, initialTarget);
    assert.equal(context.pages().length, 2);
  });
  await check('controlled fixture Brain restart reconnects its event stream without polling or a new tab', async () => {
    const nonce = await page.evaluate(() => documentNonce);
    const oldRequests = brainRequests;
    await stopBrain(); brainEpoch++; await startBrain(localPort);
    await page.waitForFunction(() => brainEvents.some((event) => event.epoch === 2), null, { timeout: 15000 });
    assert.equal(await page.evaluate(() => documentNonce), nonce);
    assert.equal(brainStarts, 2); assert.equal(brainRequests - oldRequests, 1);
    assert.equal(context.pages().length, 2);
    report.evidence.brain_restart = { server_starts: brainStarts, reconnect_requests: brainRequests - oldRequests, new_epoch: brainEpoch };
  });
  await check('worker restart restores durable draft and reuses the current trusted page', async () => {
    const oldWorker = await worker.evaluate(() => workerNonce);
    const internals = await context.newPage(); await internals.goto('chrome://serviceworker-internals/');
    assert.match(await internals.locator('body').innerText(), new RegExp(`chrome-extension://${extensionId}/`));
    await internals.getByRole('button', { name: 'Stop', exact: true }).click();
    await internals.waitForFunction(() => document.body.innerText.includes('STOPPED'));
    await internals.close();
    const reply = await page.evaluate(() => request('read'));
    assert.equal(reply.ok, true); assert.equal(reply.value.draft.terms_checked, true);
    worker = context.serviceWorkers().find((item) => item.url().startsWith(`chrome-extension://${extensionId}/`))
      ?? await context.waitForEvent('serviceworker', { timeout: 10000 });
    assert.notEqual(await worker.evaluate(() => workerNonce), oldWorker);
    report.evidence.worker_restart = { stop: 'Chrome serviceworker-internals Stop control', stopped_status_observed: true, worker_nonce_changed: true };
    await page.evaluate(() => request('navigate', { route: 'bridge' }));
    await page.waitForURL(`${routes.bridge}#journey=${journey}`);
    assert.equal((await pageSession.send('Target.getTargetInfo')).targetInfo.targetId, initialTarget);
  });
  await check('repeated explicit toolbar opens reuse the workspace without resetting its current route', async () => {
    await cdp.send('Extensions.triggerAction', { id: extensionId, targetId: userTarget });
    await cdp.send('Extensions.triggerAction', { id: extensionId, targetId: userTarget });
    await page.waitForFunction(() => document.hasFocus());
    assert.equal(page.url(), `${routes.bridge}#journey=${journey}`);
    assert.equal(context.pages().length, 2);
  });
  await check('closing and reopening the workspace restores its draft without modifying a user tab', async () => {
    await page.close();
    const newPage = context.waitForEvent('page');
    await cdp.send('Extensions.triggerAction', { id: extensionId, targetId: userTarget }); page = await newPage;
    await page.waitForURL(extensionUrl); await page.waitForFunction(() => globalThis.loaded);
    assert.equal(await page.locator('#terms').isChecked(), true);
    assert.equal(context.pages().length, 2);
  });
  await check('existing OnlyFans tab retains its document and unsaved draft with zero navigation or reload', async () => {
    assert.equal(userNavigations, 0); assert.equal(await userTab.evaluate(() => nonce), userNonce);
    assert.equal(await userTab.locator('textarea').inputValue(), 'Unsaved draft');
    report.evidence.existing_user_tab = { navigations_or_reloads: userNavigations, unsaved_draft_preserved: true };
  });
  report.evidence.package_csp = "script-src 'self'; object-src 'self'";
  report.evidence.source_sha256 = createHash('sha256').update(await readFile(path.join(here, '../runtime/onboarding-workspace.mjs'))).digest('hex');
  report.evidence.harness_sha256 = createHash('sha256').update(await readFile(fileURLToPath(import.meta.url))).digest('hex');
  report.evidence.bundle_sha256 = createHash('sha256').update(await readFile(path.join(packaged, 'background.js'))).digest('hex');
  report.evidence.permissions = ['storage'];
  report.evidence.host_permissions = ['http://bridge.localhost/*', 'https://onboarding.example.test/*'];
  report.limitations.push('Existing pre-install documents cannot be assumed to gain chrome.runtime. This package automatically navigates only the exact registered setup tab to its extension route; it never reloads OnlyFans.',
    'The first-party origin permission is necessary to find and validate pre-install workspace tabs without a broad tabs permission. Production manifest and customer permission-copy review belong to integration.',
    'Controlled restart uses a real stopped/restarted HTTP event-stream fixture, not the Windows Brain executable. Authentication/session re-establishment, installed app protocol and production shutdown ordering remain integration gates.');
  console.log(JSON.stringify(report, null, 2));
  if (args.includes('--report')) { const target = path.resolve(option('--report')); await mkdir(path.dirname(target), { recursive: true }); await writeFile(target, `${JSON.stringify(report, null, 2)}\n`); }
} finally {
  await context?.close();
  if (server?.listening) await stopBrain();
  if (!path.resolve(root).startsWith(path.resolve(tmpdir()) + path.sep)) throw Error('unsafe_temporary_directory');
  await rm(root, { recursive: true, force: true });
}
