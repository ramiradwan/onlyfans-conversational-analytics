import assert from 'node:assert/strict';
import { test } from 'node:test';
import { previewCounts } from '../ui/preview-counts.mjs';

test('daily counts exclude earlier days and retain the owner calendar', () => {
  const current = { day: '2026-10-10', message_observations: 3, inbound_observations: 2, outbound_observations: 1 };
  const result = previewCounts({ message_observations: 103, days: [
    { day: '2026-10-09', message_observations: 100 }, current,
  ] }, new Date('2026-10-10T23:50:00Z'));
  assert.equal(result.label, 'Today (UTC)');
  assert.deepEqual(result.counts, current);
  assert.deepEqual(previewCounts({ days: [] }, new Date('2026-10-10')).counts, {});
});

test('older owner responses keep their actual seven-day aggregate label', () => {
  assert.deepEqual(previewCounts({ message_observations: 103 }), {
    label: 'Last seven days', counts: { message_observations: 103 },
  });
});
