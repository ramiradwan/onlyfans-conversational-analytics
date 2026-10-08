import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { BRAIN_ORIGIN } from '../lib/brain.mjs';
import { ProvisioningHost, launchProvisioningBrowser, returnToProvisioningPage } from '../lib/provisioning-host.mjs';
import { EXTENSION_DIST, assertBuiltExtension, assertBuiltSpa } from '../lib/paths.mjs';

async function auditedExtensionId() {
  assertBuiltExtension();
  const meta = JSON.parse(await readFile(path.join(EXTENSION_DIST, 'build-meta.json'), 'utf8'));
  if (!/^[a-p]{32}$/.test(meta.extension_id ?? '')) {
    throw new Error('The audited extension artifact does not name a valid extension ID.');
  }
  return meta.extension_id;
}

async function installHostedApprovalFixture(context, hostedUrl) {
  const origin = new URL(hostedUrl).origin;
  await context.route(`${origin}/**`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: '<!doctype html><title>Secure creator approval</title><h1>Secure creator approval</h1>',
    });
  });
}

async function enterProvisioning(page, provisioning) {
  const code = await provisioning.issueHandoffCode();
  await page.goto(
    `${BRAIN_ORIGIN}/provisioning/handoff?code=${encodeURIComponent(code)}`,
    { waitUntil: 'domcontentloaded' },
  );
}

test('first-run setup preserves authoritative creator approval across return and browser restart', async () => {
  test.slow();
  assertBuiltSpa();
  const extensionId = await auditedExtensionId();
  const temporaryRoot = await realpath(await mkdtemp(path.join(tmpdir(), 'ofca-provisioning-resume-')));
  const dataDirectory = path.join(temporaryRoot, 'installation');
  const browserProfile = path.join(temporaryRoot, 'browser-profile');
  const provisioning = new ProvisioningHost({ dataDirectory, extensionId });
  let context = null;
  let page = null;
  let approvalAvailable = false;
  let acquisitions = 0;
  let finalizations = 0;
  const installApprovalResponses = async (browser) => {
    await browser.route(`${BRAIN_ORIGIN}/api/v1/provisioning/creator-association/acquire`, async (route) => {
      acquisitions += 1;
      expect(route.request().method()).toBe('POST');
      expect(route.request().headers()['x-provisioning-csrf']).toBeTruthy();
      if (approvalAvailable) return route.continue();
      return route.fulfill({ status: 409, json: { reason: 'binding_acquisition_unavailable' } });
    });
    await browser.route(`${BRAIN_ORIGIN}/api/v1/provisioning/finalize`, async (route) => {
      finalizations += 1;
      expect(approvalAvailable).toBe(true);
      if (finalizations === 1) return route.fulfill({ status: 503, json: { reason: 'membership_refresh_unavailable' } });
      return route.continue();
    });
  };

  try {
    const descriptor = await provisioning.start();
    context = await launchProvisioningBrowser(browserProfile, {
      creatorAccountId: descriptor.creator_account_id,
    });
    await installHostedApprovalFixture(context, descriptor.hosted_onboarding_url);
    await installApprovalResponses(context);
    page = await context.newPage();

    await enterProvisioning(page, provisioning);

    await test.step('registration survives browser reload without replaying the setup code', async () => {
      await page.locator('#claim-package').fill(descriptor.claim_package);
      await page.locator('#claim-submit').click();
      await expect(page.locator('#claim-step')).toHaveAttribute('data-state', 'completed');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('#claim-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#identity-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#claim-submit')).toBeDisabled();
      await expect(page.locator('#confirm-identity')).toBeEnabled();
    });

    await test.step('creator confirmation resumes at pending approval without exposing coordinates', async () => {
      await page.locator('#confirm-identity').click();
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'current');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('#identity-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#continue-creator-approval')).toBeVisible();
      await expect(page.locator('#continue-creator-approval')).toHaveAttribute('href', descriptor.hosted_onboarding_url);
      await expect(page.locator('#acquire-association')).toBeHidden();
      await expect(page.locator('#identity-step .step-state')).toHaveText('Step 2 of 4');
      await expect(page.locator('[data-rail-step="1"]')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('body')).not.toContainText(descriptor.creator_account_id);
      await expect(page.locator('body')).not.toContainText(descriptor.installation_id);
      await expect(page.locator('body')).not.toContainText(descriptor.organization_id);
    });

    await test.step('opening and returning from hosted approval does not itself approve the account', async () => {
      const [hostedPage] = await Promise.all([
        context.waitForEvent('page'),
        page.locator('#continue-creator-approval').click(),
      ]);
      await hostedPage.waitForLoadState('domcontentloaded');
      await expect(hostedPage).toHaveURL(descriptor.hosted_onboarding_url);
      await expect(hostedPage.getByRole('heading', { name: 'Secure creator approval' })).toBeVisible();
      await hostedPage.bringToFront();

      await hostedPage.close();
      await returnToProvisioningPage(page);
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#finalize-step')).toHaveAttribute('data-state', 'locked');
      await expect(page.locator('#finalize-provisioning')).toBeDisabled();
      await expect.poll(() => acquisitions).toBeGreaterThan(0);
      await expect(page.locator('#acquire-association')).toBeVisible();
      expect(finalizations).toBe(0);
      await expect(page.locator('#binding-step-description')).toBeVisible();
    });

    await test.step('pending approval survives browser restart through a fresh secure handoff', async () => {
      await context.close();
      context = await launchProvisioningBrowser(browserProfile, {
        creatorAccountId: descriptor.creator_account_id,
      });
      await installHostedApprovalFixture(context, descriptor.hosted_onboarding_url);
      await installApprovalResponses(context);
      page = await context.newPage();

      await enterProvisioning(page, provisioning);
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#continue-creator-approval')).toBeVisible();
      await expect(page.locator('#acquire-association')).toBeEnabled();
      await expect(page.locator('#finalize-provisioning')).toBeDisabled();
      expect(finalizations).toBe(0);
    });

    await test.step('authoritative approval acquisition advances durable state to finalization', async () => {
      approvalAvailable = true;
      const [hostedPage] = await Promise.all([
        context.waitForEvent('page'), page.locator('#continue-creator-approval').click(),
      ]);
      await hostedPage.waitForLoadState('domcontentloaded');
      await expect(hostedPage).toHaveURL(descriptor.hosted_onboarding_url);
      await hostedPage.bringToFront();

      await hostedPage.close();
      await returnToProvisioningPage(page);
      await expect(page.locator('#finalize-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#continue-creator-approval')).not.toBeVisible();
      await expect(page.locator('#finalize-provisioning')).toBeVisible();
      await expect(page.locator('#finalize-provisioning')).toBeEnabled();
      expect(finalizations).toBe(1);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#finalize-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#finalize-provisioning')).toBeHidden();
      await expect(page.locator('#provisioning-status')).toHaveText('The desktop app is restarting. Continue there when it opens.');
      expect(finalizations).toBe(2);
    });
  } finally {
    await context?.close().catch(() => undefined);
    await provisioning.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
