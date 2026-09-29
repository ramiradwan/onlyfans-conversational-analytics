import {readFileSync, readdirSync} from 'node:fs';
import {resolve} from 'node:path';
import {expect, it} from 'vitest';
import * as protocol from '../src/protocol/parse';

const root = resolve(process.cwd(), '..', 'shared/fixtures/protocol/v2/catchup');
for (const file of readdirSync(root)) {
  const value = JSON.parse(readFileSync(resolve(root, file), 'utf8'));
  it(`accepts catch-up fixture ${file}`, () => {
    if (value.type?.startsWith('state.') || value.type === 'bridge.session') expect(protocol.parseBrainToBridgeMessage(value, {catchupFreshness: true})).toBeTruthy();
    else if (value.type === 'bridge.hello') expect(protocol.parseBridgeToBrainMessage(value)).toBeTruthy();
    else if (value.type === 'agent.session') expect(protocol.parseBrainToAgentMessage(value)).toBeTruthy();
    else if (value.type) expect(protocol.parseAgentToBrainMessage(value)).toBeTruthy();
    else if (value.operation === 'capture.state.report') expect(protocol.parseCaptureStateReportRequest(value)).toBeTruthy();
    else if (value.operation === 'history.check.begin') expect(protocol.parseHistoryCheckBeginRequest(value)).toBeTruthy();
    else if (value.result) expect(protocol.parseHistoryCheckBeginResponse(value)).toBeTruthy();
    else expect(protocol.parseCaptureStateReportResponse(value)).toBeTruthy();
  });
}

it('keeps the default strict parser on old shapes', () => {
  for (const kind of ['state.snapshot', 'state.delta']) {
    const oldValue: unknown = JSON.parse(readFileSync(resolve(root, `old-${kind}.json`), 'utf8'));
    const newValue: unknown = JSON.parse(readFileSync(resolve(root, `${kind}.json`), 'utf8'));
    expect(protocol.parseBrainToBridgeMessage(oldValue)).toBeTruthy();
    expect(() => protocol.parseBrainToBridgeMessage(newValue)).toThrow();
  }
});
