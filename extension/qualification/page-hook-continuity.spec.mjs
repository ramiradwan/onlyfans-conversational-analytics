import { readFile } from 'node:fs/promises';
import { expect, test } from '@playwright/test';
import { SyntheticPlatform } from '../../tools/e2e-capture/fixtures/synthetic-platform.mjs';

test('the built observer retains one original socket across reinjection and same-account navigation', async ({ context, page }) => {
  const platform = new SyntheticPlatform();
  await platform.install(context);
  const hook = await readFile(new URL('../dist/page-hook.js', import.meta.url), 'utf8');
  await context.addInitScript({ content: `
    globalThis.__OFCA_CAPTURE_MODE__ = 'full';
    globalThis.fixtureCaptures = [];
    window.addEventListener('message', event => {
      if (event.source === window && event.data?.observation?.record) {
        fixtureCaptures.push(event.data.observation);
      }
    });
    ${hook}
  ` });
  await page.goto('https://onlyfans.com/my/chats');
  const documentToken = await page.evaluate(() => globalThis.fixtureDocumentToken);
  const count = () => page.evaluate(() => fixtureCaptures.length);
  const identify = () => page.evaluate(() => fixtureRead('/api2/v2/users/me'));
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
  expect(await page.evaluate(() => new Set(fixtureCaptures.map(c => c.page_epoch)).size)).toBe(6);
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

  await page.evaluate(() => window.postMessage({ type: 'ofca.capture.control', version: 1, action: 'stop' }, location.origin));
  await expect.poll(() => page.evaluate(() => globalThis.__OFCA_PAGE_HOOK_CONTROLLER__ === undefined)).toBe(true);
  platform.sendInitialMessageOnlyPeer();
  await identify();
  expect(await count()).toBe(7);
  expect(await page.evaluate(() => fixtureDocumentToken)).toBe(documentToken);
  expect(platform.httpReads.filter(path => path === '/my/chats')).toHaveLength(1);
  platform.assertFailClosed();
});
