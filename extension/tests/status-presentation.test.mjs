import assert from 'node:assert/strict';
import test from 'node:test';
import * as presentation from '../ui/presentation.mjs';
import { openCreatorAccount } from '../ui/actions.mjs';

const model = () => ({ status: { consent: { mode: 'full' }, phase: 'full', reload_required: false,
  delivery: { transport_state: 'authenticated' } }, pairing: { state: 'paired' }, desktopRuntimeReachable: true,
  legal: { configured: true, requires_reauthorization: false }, analysisReadiness: { commercial_authority: 'active', analysis_admission: 'admitted' } });

test('popup readiness uses one precedence and never says Ready while disconnected', () => {
  assert.equal(typeof presentation.statusPresentation, 'function');
  assert.equal(presentation.statusPresentation(model()).label, 'Ready');
  for (const transport_state of ['disconnected', 'connecting', 'unauthenticated']) {
    const value = model(); value.status.delivery.transport_state = transport_state;
    assert.notEqual(presentation.statusPresentation(value).label, 'Ready');
  }
  const value = model();
  value.desktopRuntimeReachable = false;
  assert.equal(presentation.statusPresentation(value).label, 'Not connected');
  value.status.consent.mode = 'paused';
  assert.equal(presentation.statusPresentation(value).label, 'Paused');
  value.status.phase = 'permission_required';
  assert.equal(presentation.statusPresentation(value).label, 'Needs access');
  value.status.observer = { helper: 'closed' };
  assert.equal(presentation.statusPresentation(value).label, 'Background tab closed');
  value.status = null;
  assert.equal(presentation.statusPresentation(value).label, 'Checking…');
});

test('desktop control applies to Full, not Preview, and requires a live control channel', () => {
  for (const [mode, resumeMode, channel, expected] of [
    ['preview', null, true, false],
    ['paused', 'preview', true, false],
    ['paused', 'full', true, true],
    ['full', null, true, true],
    ['full', null, false, false],
  ]) {
    const value = model();
    value.status.consent = { mode, resume_mode: resumeMode };
    value.pairing.desktop_control = channel;
    assert.equal(presentation.desktopOwnsCapture(value), expected);
  }
});

test('paired status distinguishes connection, activation and analysis without guessing', () => {
  for (const [transport, authority, admission, expected] of [
    ['connecting', 'active', 'admitted', 'Connecting'],
    ['disconnected', 'active', 'admitted', 'Not connected'],
    ['authenticated', 'required', 'blocked', 'Activation needed'],
    ['authenticated', 'unavailable', 'blocked', 'Needs attention'],
    ['authenticated', 'unknown', 'blocked', 'Checking activation'],
    ['authenticated', 'active', 'blocked', 'Not ready'],
    ['authenticated', 'active', 'unknown', 'Checking analysis'],
  ]) {
    const value = model();
    value.status.delivery.transport_state = transport;
    value.analysisReadiness = { commercial_authority: authority, analysis_admission: admission };
    const presentationState = presentation.statusPresentation(value);
    assert.equal(presentationState.label, expected);
    assert.doesNotMatch(presentationState.body, /saved data is unchanged/i);
  }
});

test('setup derives the current customer journey from confirmed readiness', () => {
  const value = model();
  value.config = { desktop_app_download_url: null };
  value.legal.flow = { stage: 'complete' };
  assert.equal(presentation.customerJourney(value).id, 'full_ready');
});

test('creator navigation only focuses an existing tab or creates a missing one', async () => {
  for (const exists of [true, false]) {
    const calls = [];
    const api = { tabs: {
      async query() { return exists ? [{ id: 7, windowId: 2, active: false }] : []; },
      async update(id, options) { calls.push(['tab', id, options]); },
      async create(options) { calls.push(['create', options]); },
    }, windows: { async update(id, options) { calls.push(['window', id, options]); } } };
    await openCreatorAccount(api);
    if (exists) assert.deepEqual(calls, [['tab', 7, { active: true }], ['window', 2, { focused: true }]]);
    else { assert.equal(calls.length, 1); assert.equal(calls[0][0], 'create'); }
  }
});

test('a failed creator-tab query or focus never creates a replacement tab', async () => {
  for (const failure of ['query', 'focus']) {
    let creates = 0;
    await openCreatorAccount({ tabs: {
      async query() { if (failure === 'query') throw new Error('Query unavailable'); return [{ id: 7 }]; },
      async update() { throw new Error('Focus unavailable'); },
      async create() { creates += 1; },
    } });
    assert.equal(creates, 0);
  }
});
