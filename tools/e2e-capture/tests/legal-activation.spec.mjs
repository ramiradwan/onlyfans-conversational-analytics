import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import {
  completePreModeLegalActions,
  configureSyntheticLegalBindings,
  enablePreviewAnalytics,
  openPopup,
  upgradePreviewToFull,
} from '../lib/consent-ui.mjs';
import {
  extensionId,
  extensionWorker,
  launchExtensionBrowser,
} from '../lib/extension-browser.mjs';
import { assertBuiltExtension } from '../lib/paths.mjs';

function modeRecords(audit) {
  return audit.filter((record) => record.record_type === 'mode_envelope');
}

async function legalAudit(worker) {
  return worker.evaluate(() => globalThis.__OFCA_LEGAL_ACTIVATION_AUDIT__());
}

async function writeRuntimeProof(proof) {
  const filename = process.env.OFCA_LEGAL_RUNTIME_PROOF_PATH;
  if (!filename) return;
  const resolved = path.resolve(filename);
  await mkdir(path.dirname(resolved), { recursive: true });
  await writeFile(resolved, `${JSON.stringify(proof, null, 2)}\n`, 'utf8');
}

test('runtime Legal flow proves Preview activation then distinct Full upgrade evidence', async () => {
  test.slow();
  assertBuiltExtension();
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), 'ofca-legal-activation-e2e-'));
  const browserProfile = path.join(temporaryRoot, 'chromium-profile');
  let context = null;
  const pageErrors = [];

  try {
    context = await launchExtensionBrowser(browserProfile);
    const worker = await extensionWorker(context);
    const actualExtensionId = extensionId(worker);
    const popup = await openPopup(context, actualExtensionId, pageErrors);

    await test.step('Terms → risk → Activate Software → Preview', async () => {
      await configureSyntheticLegalBindings(worker, popup);
      await completePreModeLegalActions(popup);
      await enablePreviewAnalytics(context, popup, worker);
      await expect(popup.locator('#mode-label')).toHaveText('Activity preview enabled');
    });

    const auditAfterPreview = await legalAudit(worker);
    const previewModes = modeRecords(auditAfterPreview);
    expect(auditAfterPreview).toHaveLength(3);
    expect(auditAfterPreview[0].legal_meaning).toBe('terms');
    expect(auditAfterPreview[1].legal_meaning).toBe('risk_disclosure');
    expect(previewModes).toHaveLength(1);
    expect(previewModes[0].envelope.selected_mode).toBe('preview');
    expect(previewModes[0].envelope.event_type).toBe('initial_activation');
    expect(previewModes[0].envelope.actions.extension_data_handling.action).toBe('preview_only');

    const previewRecordBeforeUpgrade = structuredClone(previewModes[0]);

    await test.step('Preview → accepted Full disclosure → Enable Full analytics', async () => {
      await upgradePreviewToFull(context, popup, worker);
      await expect(popup.locator('#mode-label')).toContainText('Full authorization saved');
    });

    const auditAfterUpgrade = await legalAudit(worker);
    const upgradedModes = modeRecords(auditAfterUpgrade);
    expect(auditAfterUpgrade).toHaveLength(4);
    expect(upgradedModes).toHaveLength(2);
    expect(upgradedModes[0]).toEqual(previewRecordBeforeUpgrade);

    const full = upgradedModes[1];
    expect(full.event_id).not.toBe(previewRecordBeforeUpgrade.event_id);
    expect(full.transaction_id).not.toBe(previewRecordBeforeUpgrade.transaction_id);
    expect(full.envelope.selected_mode).toBe('full');
    expect(full.envelope.event_type).toBe('mode_upgrade');
    expect(full.envelope.actions.extension_data_handling.action).toBe('affirmatively_authorized');
    expect(full.envelope.actions.terms.action).toBe('previously_accepted');
    expect(full.envelope.actions.risk_disclosure.action).toBe('previously_acknowledged');
    expect(full.envelope.actions.terms.timestamp)
      .toBe(previewRecordBeforeUpgrade.envelope.actions.terms.timestamp);
    expect(full.envelope.actions.risk_disclosure.timestamp)
      .toBe(previewRecordBeforeUpgrade.envelope.actions.risk_disclosure.timestamp);
    expect(Date.parse(full.envelope.occurred_at))
      .toBeGreaterThanOrEqual(Date.parse(previewRecordBeforeUpgrade.envelope.occurred_at));

    expect(pageErrors).toEqual([]);
    await writeRuntimeProof({
      schema: 'ofca-legal-activation-runtime-proof/v1',
      product_revision: process.env.OFCA_PRODUCT_REVISION ?? null,
      synthetic_instrument_bindings: true,
      extension_id: actualExtensionId,
      user_flow: [
        'Terms acceptance',
        'Risk acknowledgment',
        'Activate Software',
        'Enable Preview',
        'Review Full analytics',
        'Accepted Full prominent disclosure',
        'Enable Full analytics',
      ],
      disclosure_assertions: [
        'message content',
        'same-computer transfer',
        'no general automatic expiry',
        'companion deletion boundary',
        'other people information',
        'Extension Privacy Notice link',
      ],
      audit_after_preview: auditAfterPreview,
      audit_after_full_upgrade: auditAfterUpgrade,
      preview_record_preserved: true,
      distinct_mode_upgrade_event: true,
      page_errors: pageErrors,
    });
  } finally {
    await context?.close();
    await rm(temporaryRoot, { recursive: true, force: true });
  }
});
