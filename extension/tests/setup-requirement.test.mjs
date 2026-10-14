import assert from 'node:assert/strict';
import test from 'node:test';
import { deriveCustomerJourney } from '../runtime/customer-journey.mjs';
import { setupRequirement } from '../ui/setup-requirement.mjs';

function requirement(overrides = {}, view = 'journey') {
  const model = { status: { consent: { mode: 'full' }, phase: 'full',
    delivery: { transport_state: 'authenticated', browser_tab_sleeping: false }, ...overrides.status },
    pairing: { state: 'paired', ...overrides.pairing }, legal: { requires_reauthorization: false, ...overrides.legal },
    desktopRuntimeReachable: true, analysisReadiness: { commercial_authority: 'active', analysis_admission: 'admitted' },
    ...overrides };
  const journey = deriveCustomerJourney(model);
  return { journey: journey.id, requirement: setupRequirement({ model, view, selected: 'full', reviewFull: true, journey }) };
}

test('activation status changes keep one requirement while the confirmed journey advances', () => {
  const readings = ['unknown', 'required', 'unavailable'].map((commercial_authority) => requirement({
    analysisReadiness: { commercial_authority, analysis_admission: 'blocked' } }));
  assert.deepEqual(readings.map(({ journey }) => journey), ['activation_checking', 'activation_required', 'activation_unavailable']);
  assert.deepEqual(readings.map(({ requirement }) => requirement), ['activation', 'activation', 'activation']);
  assert.equal(requirement().requirement, 'analytics-ready');
});

test('desktop reachability and transport loss share a connection requirement', () => {
  const readings = [requirement({ desktopRuntimeReachable: false, pairing: { state: 'unpaired' } }),
    requirement({ desktopRuntimeReachable: false }),
    requirement({ status: { consent: { mode: 'full' }, delivery: { transport_state: 'disconnected' } } })];
  assert.deepEqual(readings.map(({ journey }) => journey), ['desktop_app_needed', 'desktop_app_unavailable', 'full_unavailable']);
  assert.deepEqual(readings.map(({ requirement }) => requirement), Array(3).fill('desktop-connection'));
});

test('pairing comparison is a new requirement; waiting and retry keep the connection requirement', () => {
  const readings = ['unpaired', 'pairing', 'pairing_failed', 'desktop_not_ready', 'compare']
    .map((state) => requirement({ pairing: { state } }));
  assert.deepEqual(readings.map(({ journey }) => journey),
    ['pairing_required', 'pairing_in_progress', 'pairing_failed', 'pairing_not_ready', 'pairing_in_progress']);
  assert.deepEqual(readings.map(({ requirement }) => requirement), ['pairing', 'pairing', 'pairing', 'pairing', 'pairing-comparison']);
});

test('site access, creator sign-in, extension status, tab activity and analysis remain separate requirements', () => {
  assert.equal(requirement({ status: { consent: { mode: 'full' }, phase: 'permission_required' } }).requirement, 'site-access');
  assert.equal(requirement({ pairing: { state: 'setup_incomplete' } }).requirement, 'creator-account');
  assert.equal(requirement({ pairing: { state: 'unavailable' } }).requirement, 'extension-connection');
  assert.equal(requirement({ status: { consent: { mode: 'full' }, delivery: {
    transport_state: 'authenticated', browser_tab_sleeping: true } } }).requirement, 'creator-tab');
  for (const analysis_admission of ['blocked', 'unknown']) assert.equal(requirement({
    analysisReadiness: { commercial_authority: 'active', analysis_admission } }).requirement, 'analysis');
});

test('a renewed disclosure review is distinct from initial consent without depending on wording', () => {
  assert.equal(requirement({}, 'agree').requirement, 'agreement:initial:full');
  assert.equal(requirement({ legal: { requires_reauthorization: true } }, 'agree').requirement, 'agreement:reauthorization:full');
  assert.equal(requirement({}, 'mode').requirement, 'mode-choice:full');
  assert.equal(requirement({}, 'access').requirement, 'site-access');
});
