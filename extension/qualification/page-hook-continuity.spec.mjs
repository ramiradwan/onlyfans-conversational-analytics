import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import { SYNTHETIC, SyntheticPlatform } from '../../tools/e2e-capture/fixtures/synthetic-platform.mjs';
import { confirmFullDocument } from '../../tools/visual-capture/page-hook-control-fixture.mjs';
import { assertCaptureContext } from '../runtime/capture-context.mjs';

test('the built observer retains one original socket across reinjection and same-account navigation', async ({ context, page }) => {
  const platform = new SyntheticPlatform();
  await platform.install(context);
  const blocked = [];
  await context.route(url => url.port === '17871', async route => {
    blocked.push(route.request().resourceType());
    await route.abort();
  });
  await context.routeWebSocket(url => url.port === '17871', socket => {
    blocked.push('websocket');
    socket.close();
  });
  const hook = await readFile(new URL('../dist/page-hook.js', import.meta.url), 'utf8');
  await context.addInitScript({ content: `
    globalThis.__OFCA_CAPTURE_MODE__ = 'full';
    globalThis.fixtureCaptures = [];
    globalThis.fixtureIdentities = [];
    globalThis.fixtureStatuses = [];
    window.addEventListener('message', event => {
      if (event.source === window && event.data?.observation?.record) {
        fixtureCaptures.push(event.data.observation);
      }
      if (event.source === window && event.data?.type === 'ofca.provisioning.identity.update') {
        fixtureIdentities.push(event.data);
      }
      if (event.source === window && event.data?.type === 'ofca.capture.control.status') {
        fixtureStatuses.push(event.data.status);
      }
    });
    ${hook}
  ` });
  await page.goto('https://onlyfans.com/my/chats');
  const documentToken = await page.evaluate(() => globalThis.fixtureDocumentToken);
  const count = () => page.evaluate(() => fixtureCaptures.length);
  const identify = () => page.evaluate(() => fixtureRead('/api2/v2/users/me'));
  const consent = { mode: 'full', consent_epoch: '10000000-0000-4000-8000-000000000008' };
  const sender = { id: 'fixture-extension', tab: { id: 7 }, frameId: 0, documentId: documentToken,
    documentLifecycle: 'active', url: page.url() };
  const boundCreatorAccountId = 'fixture-desktop-account';
  const authorize = async () => {
    const identity = await page.evaluate(() => fixtureIdentities.at(-1) ?? null);
    const id = identity?.authenticated_profile?.creator_account_id;
    return () => assertCaptureContext({
      sender, extensionId: sender.id, boundCreatorAccountId, consent,
      configuration: { creator_account_id: boundCreatorAccountId,
        history_acquisition: { authorized_platform_creator_id: SYNTHETIC.creatorId } },
      context: identity && { observed_platform_id: id, page_epoch: identity.page_epoch,
        sender_key: `7:${documentToken}`, consent_epoch: consent.consent_epoch },
      delivery: { consent_epoch: consent.consent_epoch },
      observation: { creator_platform_user_id: id, page_epoch: identity?.page_epoch },
    });
  };
  await expect.poll(() => page.evaluate(() => fixtureStatuses.at(-1)?.active)).toBe(true);
  await expect.poll(() => page.evaluate(() => fixtureStatuses.at(-1)?.forwarding)).toBe(false);
  await identify();
  expect(await page.evaluate(() => fixtureIdentities.length)).toBe(0);
  expect(await count()).toBe(0);
  assert.throws(await authorize(), { code: 'stale_capture_context' });
  await confirmFullDocument(page);
  await expect.poll(() => page.evaluate(() => fixtureIdentities.length)).toBe(1);
  (await authorize())();
  const capturedIdentity = () => page.evaluate(() => {
    return new Promise(resolve => {
      const listener = event => {
        if (event.source === window && event.data?.type === 'ofca.provisioning.identity.update') {
          window.removeEventListener('message', listener);
          resolve(true);
        }
      };
      window.addEventListener('message', listener);
      void fixtureRead('/api2/v2/users/me');
    });
  });

  await page.evaluate(() => fixtureOpenSocket());
  await capturedIdentity();
  platform.sendInitialMessageOnlyPeer();
  await expect.poll(count).toBe(1);
  await page.evaluate(() => {
    globalThis.fixtureObservedFetch = window.fetch;
    globalThis.fixtureObservedSocket = window.WebSocket;
  });
  for (let index = 0; index < 5; index += 1) {
    await page.addScriptTag({ content: `globalThis.__OFCA_CAPTURE_MODE__ = 'full';\n${hook}` });
    expect(await page.evaluate(() => window.fetch === fixtureObservedFetch
      && window.WebSocket === fixtureObservedSocket)).toBe(true);
    await page.evaluate(index => history.pushState(null, '', `/my/chats/chat/synthetic-${index}`), index);
    await capturedIdentity();
    platform.sendInitialMessageOnlyPeer();
    await expect.poll(count).toBe(index + 2);
  }
  // Same-document navigation retains the confirmed capture epoch (ADR 0047).
  const confirmedEpoch = await page.evaluate(() => fixtureIdentities.at(-1).page_epoch);
  expect(await page.evaluate(() => [...new Set(fixtureIdentities.map(i => i.page_epoch))]))
    .toEqual([confirmedEpoch]);
  expect(await page.evaluate(() => [...new Set(fixtureCaptures.map(c => c.page_epoch))]))
    .toEqual([confirmedEpoch]);
  expect(platform.openSockets.size).toBe(1);

  const refusedIdentity = route => route.fulfill({ status: 401, contentType: 'application/json', body: '{}' });
  await page.route('**/api2/v2/users/me', refusedIdentity);
  await page.evaluate(() => new Promise(resolve => {
    const listener = event => {
      if (event.source === window && event.data?.type === 'ofca.provisioning.identity.update'
        && event.data.authenticated_profile === null) {
        window.removeEventListener('message', listener);
        resolve(true);
      }
    };
    window.addEventListener('message', listener);
    void fetch('/api2/v2/users/me');
  }));
  await page.unroute('**/api2/v2/users/me', refusedIdentity);
  await capturedIdentity();
  platform.sendInitialMessageOnlyPeer();
  // A following HTTP capture provides an ordered observation barrier.
  await page.evaluate(() => fixtureRead('/api2/v2/chats'));
  await expect.poll(count).toBe(7);
  expect(await page.evaluate(() => fixtureCaptures.filter(c => c.event_type === 'message.observed').length)).toBe(6);
  // Sign-out evidence rotates the epoch, so later captures bind to the new confirmation.
  const renewedEpoch = await page.evaluate(() => fixtureIdentities.at(-1).page_epoch);
  expect(renewedEpoch).not.toBe(confirmedEpoch);
  expect(await page.evaluate(() => fixtureCaptures.at(-1).page_epoch)).toBe(renewedEpoch);

  const wrongAccount = 'synthetic-other-creator';
  const changedIdentity = route => route.fulfill({ status: 200, contentType: 'application/json', json: { id: wrongAccount } });
  await page.route('**/api2/v2/users/me', changedIdentity);
  await identify();
  await expect.poll(() => page.evaluate(id => fixtureIdentities.at(-1)?.authenticated_profile?.creator_account_id === id, wrongAccount)).toBe(true);
  assert.throws(await authorize(), { code: 'account_mismatch' });
  await page.unroute('**/api2/v2/users/me', changedIdentity);

  await page.evaluate(() => window.postMessage({ type: 'ofca.capture.control', version: 1, action: 'stop' }, location.origin));
  await expect.poll(() => page.evaluate(() => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__ === undefined)).toBe(true);
  platform.sendInitialMessageOnlyPeer();
  await identify();
  expect(await count()).toBe(7);
  expect(await page.evaluate(() => fixtureDocumentToken)).toBe(documentToken);
  expect(platform.httpReads.filter(path => path === '/my/chats')).toHaveLength(1);
  expect(context.pages()).toHaveLength(1);
  expect(blocked).toEqual([]);
  platform.assertFailClosed();
});
