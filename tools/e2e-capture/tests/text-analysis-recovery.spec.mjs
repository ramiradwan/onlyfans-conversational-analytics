import { expect, test } from '@playwright/test';

import { assertJournalOrder, assertOccurrences, check, dropNextReply, openSaved, prepareProfile, readJson, ready,
  sameMeaning, saved, selectProfile, stats, withTextAnalysis } from '../lib/text-analysis-browser.mjs';

test('original requests recover across lost replies and Bridge reconnection', async () => {
  await withTextAnalysis('joint', async (x) => {
    await prepareProfile(x); await ready(x.page, x.log, '/analytics');
    await selectProfile(x.page, 'Approved preview profile', 'joint');
    const lostPrepare = await dropNextReply(x.page, '/api/v1/nlp/previews');
    await x.page.getByRole('button', { name: 'Prepare preview scope', exact: true }).click();
    const forwarded = await lostPrepare.received; await lostPrepare.close();
    const original = forwarded.value;
    check(original.receipt.state === 'prepared' && original.request.external_key === forwarded.body.external_key, 'lost_prepare_identity');
    await expect(x.page.getByText('The preview outcome is unknown. Check the original request or explicitly retry its retained meaning.', { exact: true })).toBeVisible();
    await expect(x.page.getByRole('button', { name: 'Prepare preview scope', exact: true })).toBeDisabled();
    check((await stats(x.page)).engine.analyze_count === 0, 'lost_prepare_analysis');
    await ready(x.page, x.log, '/analytics');
    const prepareRecovery = x.page.locator('[data-journey-state="textanalysis.preview_recovery"]').filter({ hasText: 'preparation' });
    await prepareRecovery.getByRole('button', { name: 'Check original preview', exact: true }).click();
    await expect(x.page.getByRole('button', { name: 'Confirm original preview', exact: true })).toBeEnabled();
    const recovered = (await readJson(x.page, `/api/v1/nlp/previews/${original.request.external_key}`)).value;
    check(sameMeaning(recovered, original), 'prepare_recovery_changed');
    const confirmationPath = `/api/v1/nlp/previews/${original.request.external_key}/confirm`;
    const lostConfirm = await dropNextReply(x.page, confirmationPath);
    await x.page.getByRole('button', { name: 'Confirm original preview', exact: true }).click();
    const confirmed = await lostConfirm.received; await lostConfirm.close();
    check(sameMeaning(confirmed.body, original.confirmation) && confirmed.value.receipt.job_id === original.receipt.job_id
      && confirmed.value.receipt.contribution_id === original.receipt.contribution_id, 'lost_confirmation_identity');
    await expect(x.page.getByText('The preview outcome is unknown. Check the original request or explicitly retry its retained meaning.', { exact: true })).toBeVisible();
    await expect.poll(async () => (await readJson(x.page, `/api/v1/nlp/previews/${original.request.external_key}`)).value?.receipt.state,
      { message: 'The original confirmed request must finish without a replacement.' }).toBe('committed');
    const committed = await saved(x.page, original.receipt.contribution_id), before = await stats(x.page);
    check(before.canonical.preview_spent === 1 && before.canonical.preview_outputs === 1 && before.canonical.client_intents === 2, 'recovery_commit');
    await x.restart();
    const confirmRecovery = x.page.locator('[data-journey-state="textanalysis.preview_recovery"]').filter({ hasText: 'confirmation' });
    await confirmRecovery.getByRole('button', { name: 'Check original preview', exact: true }).click();
    await expect(x.page.getByLabel('Verified shown preview', { exact: true })).toBeVisible();
    check(sameMeaning(await saved(x.page, original.receipt.contribution_id), committed), 'reconnected_saved_changed');
    const reconnected = await stats(x.page);
    check(reconnected.engine.analyze_count === 0 && reconnected.engine.prepare_count === 0
      && reconnected.canonical.preview_spent === before.canonical.preview_spent && reconnected.canonical.client_intents === before.canonical.client_intents, 'reconnect_reexecution');
    await ready(x.page, x.log);
    await x.page.getByRole('button', { name: `Remove ${reconnected.profile_id}`, exact: true }).click();
    const remove = x.page.getByRole('dialog', { name: 'Remove installed profile?', exact: true });
    await remove.getByRole('button', { name: 'Remove profile', exact: true }).click(); await expect(remove).toHaveCount(0);
    await expect.poll(async () => {
      const result = await readJson(x.page, '/api/v1/nlp/dependencies');
      return result.status === 200 && result.value.profiles.every((profile) => profile.profile_sha256 !== reconnected.profile_sha256 || !profile.installed);
    }, { message: 'The original profile must actually be removed.' }).toBe(true);
    await x.restart('saved-only'); await ready(x.page, x.log, '/analytics');
    await openSaved(x.page);
    const retained = await saved(x.page, original.receipt.contribution_id);
    check(sameMeaning(retained, committed), 'unavailable_saved_changed');
    await assertOccurrences(x.page, retained);
    const after = await stats(x.page);
    check(after.engine.analyze_count === 0 && after.engine.prepare_count === 0 && after.canonical.preview_spent === before.canonical.preview_spent
      && after.canonical.preview_outputs === before.canonical.preview_outputs && after.canonical.client_intents === before.canonical.client_intents
      && after.canonical.committed_jobs === before.canonical.committed_jobs && after.canonical.members === before.canonical.members, 'saved_only_reexecution');
    await x.log.settle(); assertJournalOrder(x.log);
    const prepares = x.log.requests.filter((item) => item.path === '/api/v1/nlp/previews');
    const confirms = x.log.requests.filter((item) => item.path === confirmationPath);
    check(prepares.length === 1 && confirms.length === 1 && prepares[0].body.external_key === original.request.external_key
      && sameMeaning(confirms[0].body, original.confirmation), 'recovery_replacement');
    check(!x.log.requests.some((item) => item.path === '/api/v1/nlp/execution'
      || item.path === '/api/v1/nlp/dependencies' && !['prepare', 'remove'].includes(item.body.action)), 'recovery_side_effect');
  });
});
