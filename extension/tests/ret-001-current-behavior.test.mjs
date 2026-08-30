import assert from 'node:assert/strict';
import test from 'node:test';

import {
  observeExtensionClearAll,
  RET001_CURRENT_BEHAVIOR_SCHEMA,
} from '../../tools/retention-evidence/execute-ret-001-extension-current-behavior.mjs';

const REVISION = '1'.repeat(40);

test('RET-001 Phase 4 observes Extension-wide clear as positive non-selective capability', async () => {
  const result = await observeExtensionClearAll(REVISION);
  assert.equal(result.schema, RET001_CURRENT_BEHAVIOR_SCHEMA);
  assert.equal(result.observation_id, 'BEH-004-EXTENSION-CLEAR-ALL');
  assert.equal(result.capability_kind, 'POSITIVE_CAPABILITY');
  assert.equal(result.factual_result, 'EXTENSION_WIDE_LOCAL_CLEAR_OBSERVED');
  assert.equal(result.facts.deleted_database_count, 2);
  assert.equal(result.facts.chrome_storage_local_cleared, true);
  assert.equal(result.facts.chrome_storage_session_cleared, true);
  assert.equal(result.facts.selective_message_delete_operation, false);
  assert.equal(result.facts.selective_conversation_delete_operation, false);
  assert.equal(result.execution_context.production_retention_or_deletion_behavior_added, false);
  assert.equal(result.execution_context.production_deletion_api_added, false);
});
