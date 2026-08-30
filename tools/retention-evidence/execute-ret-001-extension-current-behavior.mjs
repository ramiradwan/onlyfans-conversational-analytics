#!/usr/bin/env node

import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

import { clearExtensionLocalData } from '../../extension/runtime/local-data.mjs';

export const RET001_CURRENT_BEHAVIOR_SCHEMA = 'ofca-ret-001-current-behavior-observation/v1';

function fakeIndexedDb(names, deleted) {
  return {
    async databases() {
      return names.map((name) => ({ name }));
    },
    deleteDatabase(name) {
      const request = { error: null, onsuccess: null, onerror: null, onblocked: null };
      queueMicrotask(() => {
        deleted.push(name);
        request.onsuccess?.();
      });
      return request;
    },
  };
}

export async function observeExtensionClearAll(productRevision) {
  const deleted = [];
  const clears = { local: 0, session: 0 };
  const indexedDb = fakeIndexedDb(
    ['onlyfans-agent-encrypted-account-v1-alpha', 'ofca_legal_evidence_v1'],
    deleted,
  );
  const chromeApi = {
    storage: {
      local: { clear: async () => { clears.local += 1; } },
      session: { clear: async () => { clears.session += 1; } },
    },
  };

  const result = await clearExtensionLocalData({ chromeApi, indexedDb });
  assert.equal(result.deleted_databases, 2);
  assert.deepEqual(deleted, [
    'onlyfans-agent-encrypted-account-v1-alpha',
    'ofca_legal_evidence_v1',
  ]);
  assert.deepEqual(clears, { local: 1, session: 1 });

  return {
    schema: RET001_CURRENT_BEHAVIOR_SCHEMA,
    observation_id: 'BEH-004-EXTENSION-CLEAR-ALL',
    product_revision: productRevision,
    observed_at: new Date().toISOString(),
    capability_kind: 'POSITIVE_CAPABILITY',
    factual_result: 'EXTENSION_WIDE_LOCAL_CLEAR_OBSERVED',
    facts: {
      enumerated_database_names: [
        'onlyfans-agent-encrypted-account-v1-alpha',
        'ofca_legal_evidence_v1',
      ],
      deleted_database_names: deleted,
      deleted_database_count: result.deleted_databases,
      chrome_storage_local_cleared: clears.local === 1,
      chrome_storage_session_cleared: clears.session === 1,
      operation_scope: 'all_enumerated_extension_indexeddb_databases_plus_local_and_session_storage',
      selective_message_delete_operation: false,
      selective_conversation_delete_operation: false,
    },
    execution_context: {
      runner: 'ret-001-extension-current-behavior/v1',
      synthetic_fixture: true,
      production_retention_or_deletion_behavior_added: false,
      production_deletion_api_added: false,
      test_only_direct_sql_deletion: false,
    },
    notes: [
      'The observation invokes the existing production clearExtensionLocalData function using test-only browser API doubles.',
      'This proves an Extension-wide local clearing capability; it does not establish a selective message or conversation deletion control.',
      'Companion stores are outside this Extension clearing boundary.',
    ],
  };
}

async function main() {
  const args = process.argv.slice(2);
  const revisionIndex = args.indexOf('--product-revision');
  const outputIndex = args.indexOf('--output-dir');
  if (revisionIndex === -1 || outputIndex === -1) {
    throw new Error('Usage: --product-revision <sha> --output-dir <dir>');
  }
  const productRevision = args[revisionIndex + 1];
  const outputDir = args[outputIndex + 1];
  if (!/^[0-9a-f]{40}$/.test(productRevision)) {
    throw new Error('--product-revision must be a lowercase 40-hex commit SHA');
  }
  const observation = await observeExtensionClearAll(productRevision);
  const destination = resolve(outputDir, `${observation.observation_id.toLowerCase()}.json`);
  await mkdir(outputDir, { recursive: true });
  await writeFile(destination, `${JSON.stringify(observation, null, 2)}\n`, 'utf8');
  process.stdout.write(`${JSON.stringify({
    schema: 'ofca-ret-001-current-behavior-extension-run/v1',
    product_revision: productRevision,
    observation_id: observation.observation_id,
    factual_result: observation.factual_result,
    output_file: destination,
  }, null, 2)}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
