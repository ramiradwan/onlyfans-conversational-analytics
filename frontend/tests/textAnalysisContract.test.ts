import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import command from '../../shared/nlp/application/v1/fixtures/enrollment-command.json';
import view from '../../shared/nlp/application/v1/fixtures/enrollment-view.json';
import commandResult from '../../shared/nlp/application/v1/fixtures/enrollment-result.json';
import prepare from '../../shared/nlp/application/v1/fixtures/preview-prepare.json';
import outcome from '../../shared/nlp/application/v1/fixtures/preview-outcome.json';
import confirmation from '../../shared/nlp/application/v1/fixtures/preview-confirmation.json';
import saved from '../../shared/nlp/application/v1/fixtures/preview-saved.json';
import partial from '../../shared/nlp/application/v1/fixtures/preview-saved-partial.json';
import enrollmentIntent from '../../shared/nlp/application/v1/fixtures/client-enrollment-intent.json';
import record from '../../shared/nlp/application/v1/fixtures/client-enrollment-record.json';
import prepareIntent from '../../shared/nlp/application/v1/fixtures/client-prepare-intent.json';
import confirmIntent from '../../shared/nlp/application/v1/fixtures/client-confirm-intent.json';
import intentPage from '../../shared/nlp/application/v1/fixtures/client-intent-page.json';
import savedPage from '../../shared/nlp/application/v1/fixtures/preview-saved-page.json';
import { clientIntent, enrollmentCommand, parseClientIntentPage, parseClientIntentRecord, parseEnrollmentCommandResult,
  parseEnrollmentView, parsePreviewOutcome, parseSavedPreview, parseSavedPreviewPage, previewConfirmation, previewPrepare, textAnalysisDigest } from '../src/analytics/textAnalysisContract';

beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => vi.unstubAllGlobals());
describe('text analysis wire contracts', () => {
  it('accepts the independently generated complete application fixtures', async () => {
    expect(enrollmentCommand.parse(command)).toEqual(command);
    expect(parseEnrollmentView(view, 'example-account')).toEqual(view);
    expect(parseEnrollmentCommandResult(commandResult, command.command_key)).toEqual(commandResult);
    expect(previewPrepare.parse(prepare)).toEqual(prepare);
    expect(previewConfirmation.parse(confirmation)).toEqual(confirmation);
    expect(parsePreviewOutcome(outcome, prepare.external_key, previewPrepare.parse(prepare))).toEqual(outcome);
    expect(await parseSavedPreview(saved, saved.output.contribution_id)).toEqual(saved);
    expect(await parseSavedPreview(partial, partial.output.contribution_id)).toEqual(partial);
    for (const input of [enrollmentIntent, prepareIntent, confirmIntent]) expect(clientIntent.parse(input)).toEqual(input);
    expect(await parseClientIntentRecord(record)).toEqual(record);
    expect(await parseClientIntentPage(intentPage)).toEqual(intentPage);
    expect(parseSavedPreviewPage(savedPage)).toEqual(savedPage);
  });
  it('normalizes outgoing defaults without accepting extra fields or changed command semantics', () => {
    const { schema_version: _schema, expected_profile_sha256: _profile, ...body } = command;
    const pause = { ...body, action: 'pause', expected_enrollment_id: view.current.enrollment_id };
    expect(enrollmentCommand.parse(pause)).toMatchObject({ schema_version: command.schema_version, expected_profile_sha256: null });
    expect(() => enrollmentCommand.parse({ ...pause, expected_profile_sha256: 'a'.repeat(64) })).toThrow();
    expect(() => enrollmentCommand.parse({ ...command, scope: 'other' })).toThrow();
    expect(() => parseEnrollmentView(view, 'another-account')).toThrow();
    expect(() => parseEnrollmentCommandResult(commandResult, crypto.randomUUID())).toThrow();
  });
  it('binds original preview keys, immutable identities, profile and disclosed scope', () => {
    for (const mutate of [
      (value: typeof outcome) => { value.request.expected_profile_sha256 = 'a'.repeat(64); },
      (value: typeof outcome) => { value.receipt.job_id = crypto.randomUUID(); },
      (value: typeof outcome) => { value.scope.lower_exclusive = value.scope.cutoff; },
      (value: typeof outcome) => { value.confirmation.input_inventory_sha256 = 'a'.repeat(64); },
    ]) { const value = structuredClone(outcome); mutate(value); expect(() => parsePreviewOutcome(value, prepare.external_key)).toThrow(); }
    expect(() => parsePreviewOutcome({ ...outcome, detailed_scope_available: false }, prepare.external_key)).toThrow();
  });
  it('rejects journal body mutation, digest substitution and key/cursor mismatch', async () => {
    await expect(parseClientIntentRecord({ ...record, body_sha256: 'a'.repeat(64) })).rejects.toThrow();
    await expect(parseClientIntentRecord({ ...record, key: crypto.randomUUID() })).rejects.toThrow();
    await expect(parseClientIntentPage({ ...intentPage, items: [...intentPage.items, ...intentPage.items] })).rejects.toThrow();
    expect(() => parseSavedPreviewPage({ ...savedPage, next_after: crypto.randomUUID() })).toThrow();
    expect(() => clientIntent.parse({ ...confirmIntent, body: { ...confirmIntent.body, external_key: crypto.randomUUID() } })).toThrow();
  });
  it('refuses fabricated coverage, totals, support, hidden metadata and original disclosure changes', async () => {
    const mutations: ((value: typeof saved) => void)[] = [
      (value) => { value.output.tasks[0].assessed += 1; },
      (value) => { value.output.examination.reverse(); },
      (value) => { value.output.summaries[0].supporting_messages += 1; },
      (value) => { value.output.summaries[0].share_of_complete_messages = 0.123; },
      (value) => { value.output.summaries[0].label.display_label = 'Different label'; },
      (value) => { value.disclosure.profile_sha256 = 'a'.repeat(64); },
      (value) => { value.disclosure.tasks.reverse(); },
      (value) => { value.output.summaries[1].evidence[0].reference.span.end = 99999; },
    ];
    for (const mutate of mutations) { const value = structuredClone(saved); mutate(value); await expect(parseSavedPreview(value, saved.output.contribution_id)).rejects.toThrow(); }
    await expect(parseSavedPreview({ ...saved, hidden_candidates: [] }, saved.output.contribution_id)).rejects.toThrow();
    const wrong = structuredClone(partial); wrong.output.summaries[0].additional_categories = 1;
    await expect(parseSavedPreview(wrong, partial.output.contribution_id)).rejects.toThrow();
  });
  it('preserves repeated exact mention spans and Python codepoint label bounds', async () => {
    const result = await parseSavedPreview(saved, saved.output.contribution_id);
    expect(result.output.summaries.find((item) => item.kind === 'mention_category')?.evidence.map((item) => item.reference.span)).toEqual([{ start: 2, end: 6 }, { start: 7, end: 11 }]);
    const value = structuredClone(saved), label = value.output.summaries[0].label;
    label.label_status = 'resolved';
    label.display_label = '😀'.repeat(256);
    const { label_identity_sha256: _digest, ...body } = label; label.label_identity_sha256 = await textAnalysisDigest(body);
    await expect(parseSavedPreview(value, value.output.contribution_id)).resolves.toBeDefined();
    label.display_label += '😀';
    const { label_identity_sha256: _changedDigest, ...changedBody } = label; label.label_identity_sha256 = await textAnalysisDigest(changedBody);
    await expect(parseSavedPreview(value, value.output.contribution_id)).rejects.toThrow();
  });
});
