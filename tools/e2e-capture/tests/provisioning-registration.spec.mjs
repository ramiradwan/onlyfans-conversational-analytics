import { readFile, mkdtemp, mkdir, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { BRAIN_ORIGIN, BrainProcess } from '../lib/brain.mjs';
import {
  completeBrowserWebAuthnCeremony,
  readServedRuntimeConfig,
} from '../lib/brain-probe.mjs';
import { ProvisioningHost, launchProvisioningBrowser } from '../lib/provisioning-host.mjs';
import {
  EXTENSION_DIST,
  assertBuiltExtension,
  assertBuiltSpa,
} from '../lib/paths.mjs';

const RUNTIME_CONFIGURATION_FILENAME = 'runtime.env';

const NONSECRET_CONFIGURATION = [
  'ANALYTICS_PROJECTION_DATABASE_PATH',
  'AUTH_DATABASE_PATH',
  'CANONICAL_DATABASE_PATH',
  'EXTENSION_ID',
  'IDENTITY_BINDING_SOURCE',
  'LOCAL_BRIDGE_ROLE',
  'LOCAL_PRINCIPAL_ID',
  'PROJECTION_DATABASE_PATH',
];

async function readRuntimeConfiguration(configurationPath) {
  const content = await readFile(configurationPath, 'utf8');
  const values = {};
  for (const line of content.split('\n')) {
    const entry = line.trim();
    if (entry.length === 0) continue;
    const boundary = entry.indexOf('=');
    if (boundary <= 0) throw new Error('Runtime configuration holds an unparsable line.');
    values[entry.slice(0, boundary)] = JSON.parse(entry.slice(boundary + 1));
  }
  return values;
}

async function auditedExtensionId() {
  assertBuiltExtension();
  const meta = JSON.parse(await readFile(path.join(EXTENSION_DIST, 'build-meta.json'), 'utf8'));
  if (!/^[a-p]{32}$/.test(meta.extension_id ?? '')) {
    throw new Error('The audited extension artifact does not name a valid extension ID.');
  }
  return meta.extension_id;
}

// The grants this stage verifies are signed by keys it generates, and every
// hosted response comes from the same process. A pass therefore establishes
// that the shipped claim decoder, grant verifier, authentication store,
// finalizer, and WebAuthn routes agree with one another. It cannot establish
// that the packaged production trust set matches the production grant signer,
// that a control plane issues a claim this build accepts, or that a TPM-backed
// platform authenticator works: the signing keys, the hosted plane, and the
// authenticator are all local to the run.
test('a clean installation registers, authenticates, and reaches its configured runtime', async () => {
  test.slow();
  assertBuiltSpa();
  const extensionId = await auditedExtensionId();

  // Resolved, because the paths provisioning writes are resolved and a short
  // 8.3 component would make every path comparison here compare spellings.
  const temporaryRoot = await realpath(await mkdtemp(path.join(tmpdir(), 'ofca-provisioning-')));
  const dataDirectory = path.join(temporaryRoot, 'installation');
  // The runtime reads its bindings from the values provisioning wrote, but
  // resolves runtime.env from this directory instead, so the activation gate
  // keeps the posture the rest of this lane runs under.
  const runtimeStateDirectory = path.join(temporaryRoot, 'runtime-state');
  const browserProfile = path.join(temporaryRoot, 'browser-profile');
  const provisioning = new ProvisioningHost({ dataDirectory, extensionId });
  const pageErrors = [];
  let context = null;
  let brain = null;

  try {
    await mkdir(runtimeStateDirectory, { recursive: true });
    const descriptor = await provisioning.start();
    expect(descriptor.claim_package).toMatch(/^[A-Za-z0-9_-]+$/);

    context = await launchProvisioningBrowser(browserProfile, {
      creatorAccountId: descriptor.creator_account_id,
    });
    const page = await context.newPage();
    page.on('pageerror', (error) => pageErrors.push(error.message));

    await test.step('the launcher handoff opens the provisioning page', async () => {
      const code = await provisioning.issueHandoffCode();
      await page.goto(
        `${BRAIN_ORIGIN}/provisioning/handoff?code=${encodeURIComponent(code)}`,
        { waitUntil: 'domcontentloaded' },
      );
      expect(new URL(page.url()).pathname).toBe('/provisioning');
      await expect(page.locator('#claim-step')).toHaveAttribute('data-state', 'current');
    });

    await test.step('the pasted claim package registers the installation', async () => {
      await page.locator('#claim-package').fill(descriptor.claim_package);
      await page.locator('#claim-submit').click();
      await expect(page.locator('#provisioning-status'))
        .toHaveText('Installation registered. Confirm the detected creator account.');
      await expect(page.locator('#claim-step')).toHaveAttribute('data-state', 'completed');
    });

    await test.step('the detected creator account is confirmed and approved', async () => {
      await expect(page.locator('#detected-identity')).toHaveText(descriptor.creator_account_id);
      await page.locator('#confirm-identity').click();
      await expect(page.locator('#provisioning-status'))
        .toHaveText('Creator account confirmed. Acquire approval to continue.');
      await page.locator('#acquire-association').click();
      await expect(page.locator('#provisioning-status'))
        .toHaveText('Approval acquired. Finish configuration to complete setup.');
    });

    let configuration = null;
    await test.step('finalization writes runtime configuration', async () => {
      await page.locator('#finalize-provisioning').click();
      await expect(page.locator('#provisioning-status'))
        .toHaveText('Configuration is complete. Restart Bridge to continue.');
      configuration = await readRuntimeConfiguration(
        path.join(dataDirectory, RUNTIME_CONFIGURATION_FILENAME),
      );
      for (const name of NONSECRET_CONFIGURATION) {
        expect(configuration[name] ?? '').not.toBe('');
      }
      expect(configuration.ENVIRONMENT).toBe('production');
      expect(configuration.IDENTITY_BINDING_SOURCE).toBe('verified_grants');
      expect(configuration.EXTENSION_ID).toBe(extensionId);
      expect(configuration.AUTH_DATABASE_PATH)
        .toBe(path.join(dataDirectory, 'auth.sqlite3'));
      expect((configuration.LOCAL_SESSION_BOOTSTRAP_TOKEN ?? '').length)
        .toBeGreaterThanOrEqual(32);
      expect((configuration.SECURITY_SIGNING_SECRET ?? '').length)
        .toBeGreaterThanOrEqual(32);
    });

    await provisioning.stop();

    await test.step('the runtime starts on the configuration provisioning wrote', async () => {
      brain = new BrainProcess({
        authDatabasePath: configuration.AUTH_DATABASE_PATH,
        canonicalDatabasePath: configuration.CANONICAL_DATABASE_PATH,
        extensionId: configuration.EXTENSION_ID,
        projectionDatabasePath: configuration.PROJECTION_DATABASE_PATH,
        environmentOverrides: {
          ANALYTICS_PROJECTION_DATABASE_PATH: configuration.ANALYTICS_PROJECTION_DATABASE_PATH,
          IDENTITY_BINDING_SOURCE: configuration.IDENTITY_BINDING_SOURCE,
          LOCAL_ANALYTICS_DATA_DIR: runtimeStateDirectory,
          LOCAL_BRIDGE_ROLE: configuration.LOCAL_BRIDGE_ROLE,
          LOCAL_PRINCIPAL_ID: configuration.LOCAL_PRINCIPAL_ID,
          LOCAL_SESSION_BOOTSTRAP_TOKEN: configuration.LOCAL_SESSION_BOOTSTRAP_TOKEN,
          SECURITY_SIGNING_SECRET: configuration.SECURITY_SIGNING_SECRET,
        },
      });
      await brain.start();
    });

    await test.step('a credential registers and authenticates against that runtime', async () => {
      await completeBrowserWebAuthnCeremony(page);
      const served = await readServedRuntimeConfig(context);
      expect(served.CREATOR_ID).toBe(descriptor.creator_account_id);
      expect(served.EXTENSION_ID).toBe(extensionId);
    });

    expect(pageErrors).toEqual([]);
  } finally {
    await brain?.stop().catch(() => undefined);
    await context?.close().catch(() => undefined);
    await provisioning.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
