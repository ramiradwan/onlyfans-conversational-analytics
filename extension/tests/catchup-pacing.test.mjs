import assert from 'node:assert/strict';
import test from 'node:test';
import { DurableIngestOutbox } from '../transport/read-only-durable-outbox.mjs';
import { HistoryAcquisitionCoordinator } from '../transport/read-only-history-coordinator.mjs';
import { InMemoryIngestionStorage } from './in-memory-ingestion-storage.mjs';
import { traversalConfiguration, TRAVERSAL_ACCOUNT } from './signer-traversal-scenario.mjs';

test('initial history repeated wakes share a persisted minute allowance', async () => {
  const storage = new InMemoryIngestionStorage();
  let clock = Date.parse('2026-09-29T12:00:00Z');
  let pages = 0;
  const create = async () => {
    const outbox = new DurableIngestOutbox({ storage, creatorAccountId: TRAVERSAL_ACCOUNT });
    await outbox.initialize();
    return new HistoryAcquisitionCoordinator({ outbox, clock: () => clock,
      now: () => new Date(clock).toISOString(), delay: async () => {},
      configuration: () => traversalConfiguration({ pages_per_wake: 2 }),
      session: () => ({ creator_account_id: TRAVERSAL_ACCOUNT, applied_config_revision: 'synthetic-config-v1' }),
      signer: { async read({ operation }) {
        if (operation === 'identity') return { success: true, operation, data: { id: '9001' } };
        pages++;
        return { success: true, operation, data: { items: [{ id: String(100 + pages),
          platform_user_id: String(100 + pages), display_name: null, updated_at: null }],
          continuation: `cursor-${pages}`, boundary: null } };
      } },
    });
  };
  const first = await create();
  for (let wake = 0; wake < 5; wake++) await first.wake();
  assert.equal(pages, 2);
  first.stop();
  const restarted = await create();
  await restarted.wake();
  assert.equal(pages, 2);
  clock += 60_000;
  await restarted.wake();
  assert.equal(pages, 4);
});
