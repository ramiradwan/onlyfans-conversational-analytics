import { mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import { BRAIN_ORIGIN } from '../lib/brain.mjs';
import { ProvisioningHost, launchProvisioningBrowser } from '../lib/provisioning-host.mjs';
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

  try {
    const descriptor = await provisioning.start();
    context = await launchProvisioningBrowser(browserProfile, {
      creatorAccountId: descriptor.creator_account_id,
    });
    await installHostedApprovalFixture(context, descriptor.hosted_onboarding_url);
    page = await context.newPage();
    const code = await provisioning.issueHandoffCode();
    await page.goto(
      `${BRAIN_ORIGIN}/provisioning/handoff?code=${encodeURIComponent(code)}`,
      { waitUntil: 'domcontentloaded' },
    );

    await test.step('registration survives browser reload without replaying the setup code', async () => {
      await page.locator('#claim-package').fill(descriptor.claim_package);
      await page.locator('#claim-submit').click();
      await expect(page.locator('#claim-step')).toHaveAttribute('data-state', 'completed');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('#claim-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#identity-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#claim-submit')).toBeDisabled();
      await expect(page.locator('#detected-identity')).toHaveText('Signed-in creator account detected');
    });

    await test.step('creator confirmation resumes at pending approval without exposing coordinates', async () => {
      await page.locator('#confirm-identity').click();
      await expect(page.locator('#provisioning-status')).toContainText('Approval is still waiting for completion');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('#identity-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#continue-creator-approval')).toBeVisible();
      await expect(page.locator('#continue-creator-approval')).toHaveAttribute('href', descriptor.hosted_onboarding_url);
      await expect(page.locator('#acquire-association')).toBeEnabled();
      await expect(page.locator('#detected-identity')).toHaveText('Creator account already confirmed');
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
      await hostedPage.close();
      await page.bringToFront();
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#finalize-step')).toHaveAttribute('data-state', 'locked');
      await expect(page.locator('#finalize-provisioning')).toBeDisabled();
      await expect(page.locator('#provisioning-status')).toContainText('Approval is still waiting for completion');
    });

    await test.step('pending approval survives closing and reopening the browser profile', async () => {
      await context.close();
      context = await launchProvisioningBrowser(browserProfile, {
        creatorAccountId: descriptor.creator_account_id,
      });
      await installHostedApprovalFixture(context, descriptor.hosted_onboarding_url);
      page = await context.newPage();
      await page.goto(`${BRAIN_ORIGIN}/provisioning`, { waitUntil: 'domcontentloaded' });
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#continue-creator-approval')).toBeVisible();
      await expect(page.locator('#acquire-association')).toBeEnabled();
      await expect(page.locator('#finalize-provisioning')).toBeDisabled();
    });

    await test.step('authoritative approval acquisition advances durable state to finalization', async () => {
      await page.locator('#acquire-association').click();
      await expect(page.locator('#provisioning-status')).toHaveText('Creator account approved. Finish desktop setup.');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(page.locator('#binding-step')).toHaveAttribute('data-state', 'completed');
      await expect(page.locator('#continue-creator-approval')).not.toBeVisible();
      await expect(page.locator('#finalize-step')).toHaveAttribute('data-state', 'current');
      await expect(page.locator('#finalize-provisioning')).toBeEnabled();
    });
  } finally {
    await context?.close().catch(() => undefined);
    await provisioning.stop().catch(() => undefined);
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
