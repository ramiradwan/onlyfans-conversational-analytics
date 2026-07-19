import { describe, expect, it } from 'vitest';

import { bindAgentToBrain } from '../src/services/extensionBinding';

describe('Agent Brain-session binding', () => {
  it('sends the exact v2 account binding without logging or extra fields', async () => {
    const calls: Array<{ extensionId: string; message: Record<string, unknown> }> = [];
    const runtime = {
      sendMessage(
        extensionId: string,
        message: Record<string, unknown>,
        callback: (response: unknown) => void,
      ) {
        calls.push({ extensionId, message });
        callback({ ok: true });
      },
    };
    await expect(bindAgentToBrain({
      extensionId: 'abcdefghijklmnopabcdefghijklmnop',
      creatorAccountId: 'creator-1',
      authTicket: 'purpose-bound-ticket',
      runtime,
    })).resolves.toEqual({ status: 'bound' });
    expect(calls).toEqual([{
      extensionId: 'abcdefghijklmnopabcdefghijklmnop',
      message: {
        type: 'ofca.agent.bind',
        protocol_version: '2',
        creator_account_id: 'creator-1',
        auth_ticket: 'purpose-bound-ticket',
      },
    }]);
  });

  it('does not attempt external messaging without a configured extension', async () => {
    await expect(bindAgentToBrain({
      extensionId: 'dev-extension-id',
      creatorAccountId: 'creator-1',
      authTicket: 'ticket',
      runtime: undefined,
    })).resolves.toEqual({ status: 'unavailable' });
  });
});
