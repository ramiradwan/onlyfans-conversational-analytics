const BINDING_MESSAGE_TYPE = 'ofca.agent.bind';

interface ChromeRuntimeLike {
  lastError?: { message?: string };
  sendMessage(
    extensionId: string,
    message: Record<string, unknown>,
    callback: (response: unknown) => void,
  ): void;
}

export type ExtensionBindingResult =
  | { status: 'bound' }
  | { status: 'unavailable' };

function browserRuntime(): ChromeRuntimeLike | undefined {
  return (globalThis as typeof globalThis & {
    chrome?: { runtime?: ChromeRuntimeLike };
  }).chrome?.runtime;
}

/** Sends one purpose-bound Agent ticket through Chrome's exact-origin external messaging gate. */
export function bindAgentToBrain({
  extensionId,
  creatorAccountId,
  authTicket,
  runtime = browserRuntime(),
}: {
  extensionId: string;
  creatorAccountId: string;
  authTicket: string;
  runtime?: ChromeRuntimeLike;
}): Promise<ExtensionBindingResult> {
  if (
    runtime === undefined
    || extensionId.length === 0
    || extensionId === 'dev-extension-id'
  ) return Promise.resolve({ status: 'unavailable' });
  if (creatorAccountId.length === 0 || authTicket.length === 0) {
    return Promise.reject(new Error('Agent binding configuration is incomplete'));
  }
  return new Promise((resolve, reject) => {
    runtime.sendMessage(extensionId, {
      type: BINDING_MESSAGE_TYPE,
      protocol_version: '2',
      creator_account_id: creatorAccountId,
      auth_ticket: authTicket,
    }, (response) => {
      if (runtime.lastError) {
        reject(new Error('The local Agent extension could not be reached'));
        return;
      }
      if (
        typeof response !== 'object'
        || response === null
        || (response as { ok?: unknown }).ok !== true
      ) {
        reject(new Error('The local Agent rejected the Brain session binding'));
        return;
      }
      resolve({ status: 'bound' });
    });
  });
}
