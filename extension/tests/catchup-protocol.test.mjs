import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import * as generic from '../protocol/index.mjs';
import * as readonly from '../protocol/read-only.mjs';

const root = fileURLToPath(new URL('../../shared/fixtures/protocol/v2/catchup/', import.meta.url));
for (const [label, protocol] of [['generic', generic], ['readonly', readonly]]) {
  for (const file of readdirSync(root)) {
    const value = JSON.parse(readFileSync(root + '/' + file, 'utf8'));
    if (label === 'readonly' && value.type === 'agent.hello') {
      value.payload.capabilities = value.payload.capabilities.filter(item => item !== 'command.message.send');
    }
    if (value.type?.startsWith('bridge.') || value.type?.startsWith('state.')) continue;
    test(`${label} accepts ${file}`, () => {
      if (value.type) {
        const parser = value.type === 'agent.session' ? protocol.parseBrainToAgentMessage : protocol.parseAgentToBrainMessage;
        assert.ok(parser(value));
      } else if (value.operation === 'capture.state.report') assert.ok(protocol.parseCaptureStateReportRequest(value));
      else if (value.operation === 'history.check.begin') assert.ok(protocol.parseHistoryCheckBeginRequest(value));
      else if (value.result) assert.ok(protocol.parseHistoryCheckBeginResponse(value));
      else assert.ok(protocol.parseCaptureStateReportResponse(value));
    });
  }
  test(`${label} rejects malformed check identity`, () => {
    const value = JSON.parse(readFileSync(root + '/ingest.delta.json', 'utf8'));
    value.payload.check_id = 42;
    assert.throws(() => protocol.parseAgentToBrainMessage(value));
  });
}
