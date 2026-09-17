import assert from 'node:assert/strict';
import test from 'node:test';

import {
  POPUP_CONTEXT_KEY,
  POPUP_CONTEXT_MAX_AGE_MS,
  loadPopupContext,
  parsePopupContext,
  savePopupContext,
} from '../runtime/popup-context.mjs';

const NOW = 1_800_000_000_000;
function area() {
  const values = {};
  return {
    values,
    async get(keys) { return Object.fromEntries(keys.filter((key) => Object.hasOwn(values, key)).map((key) => [key, structuredClone(values[key])])); },
    async set(update) { Object.assign(values, structuredClone(update)); },
  };
}

test('a saved view and review position restore within the resume window', async () => {
  const storage = area();
  await savePopupContext(storage, { view: 'connection', full_review_requested: true, initial_choice_dismissed: true }, NOW);
  assert.deepEqual(Object.keys(storage.values), [POPUP_CONTEXT_KEY]);
  assert.deepEqual(await loadPopupContext(storage, NOW + 60_000), {
    view: 'connection', full_review_requested: true, initial_choice_dismissed: true,
  });
});

test('an expired context returns home but keeps the session dismissal', () => {
  const saved = { view: 'manage', full_review_requested: true, initial_choice_dismissed: true, saved_at: NOW };
  assert.deepEqual(parsePopupContext(saved, NOW + POPUP_CONTEXT_MAX_AGE_MS + 1), {
    view: 'home', full_review_requested: false, initial_choice_dismissed: true,
  });
  assert.equal(parsePopupContext(saved, NOW - 1).view, 'home');
});

test('only the exact presentation shape is accepted', () => {
  const base = { view: 'connection', full_review_requested: false, initial_choice_dismissed: false, saved_at: NOW };
  const empty = { view: 'home', full_review_requested: false, initial_choice_dismissed: false };
  for (const candidate of [
    null, [], 'connection',
    { ...base, feedback: 'Full setup started.' },
    { ...base, pairing: 'paired' },
    { ...base, view: 'pairing' },
    { ...base, full_review_requested: 'true' },
    { ...base, saved_at: String(NOW) },
  ]) assert.deepEqual(parsePopupContext(candidate, NOW), empty);
  assert.equal(parsePopupContext(base, NOW).view, 'connection');
});

test('storage failures fall back to the default popup', async () => {
  const broken = { async get() { throw new Error('unavailable'); }, async set() { throw new Error('unavailable'); } };
  assert.equal((await loadPopupContext(broken, NOW)).view, 'home');
  await savePopupContext(broken, { view: 'manage' }, NOW);
});
