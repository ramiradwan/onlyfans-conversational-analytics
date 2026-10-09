import type { RuntimeLike } from './extensionPort';
import type { OnboardingAdapter, OnboardingCommand } from '../../../shared/onboarding/client.mjs';
import { validateOnboardingMessage } from '../protocol/onboarding';


export const ONBOARDING_PORT_NAME = 'ofca.onboarding.v1';
const DEADLINE_MS = 10_000;
const EXTENSION_ID = /^[a-p]{32}$/u;

/** Chrome authenticates the selected extension; the worker admits the workspace tab. */
export function createOnboardingExtensionAdapter({ runtime, extensionId, onDisconnect = () => {} }: {
  runtime: RuntimeLike; extensionId: string; onDisconnect?: () => void;
}): OnboardingAdapter {
  let port: ReturnType<RuntimeLike['connect']> | null = null;
  let negotiated = false;
  let closed = false;
  let next: ((message: unknown) => void) | null = null;
  let dropped: (() => void) | null = null;
  let pending: { resolve(value: unknown): void; reject(): void; timer: ReturnType<typeof setTimeout> } | null = null;
  const close = () => {
    if (closed) return;
    closed = true;
    negotiated = false;
    if (pending) { clearTimeout(pending.timer); pending.reject(); pending = null; }
    try { port?.disconnect(); } catch { /* Already disconnected. */ }
    dropped?.();
    onDisconnect();
  };
  const send = (value: unknown) => {
    if (closed || !port) throw new Error('Onboarding connection unavailable');
    port.postMessage(value);
  };
  return {
    subscribe(receive, disconnect) {
      if (!EXTENSION_ID.test(extensionId) || port || closed) throw new Error('Invalid onboarding connection');
      next = receive; dropped = disconnect;
      port = runtime.connect(extensionId, { name: ONBOARDING_PORT_NAME });
      port.onMessage.addListener((value: unknown) => {
        if (closed) return;
        if (typeof value !== 'object' || value === null) return;
        if ('type' in value && value.type === 'capabilities') {
          if (Object.keys(value).length !== 2 || !('capabilities' in value)
            || !Array.isArray(value.capabilities) || value.capabilities.length > 16
            || !value.capabilities.every((item) => typeof item === 'string' && item.length <= 64)
            || !value.capabilities.includes('local-onboarding.v1')
            || !value.capabilities.includes('local-onboarding.command-result.v2')
            || !value.capabilities.includes('persistent-workspace.v1')) { close(); return; }
          negotiated = true;
          if (pending) { try { send({ type: 'snapshot' }); } catch { close(); } }
          return;
        }
        if (!negotiated || !validateOnboardingMessage(value) || !('source' in value) || value.source !== 'extension') return;
        if ('kind' in value && value.kind === 'snapshot' && pending) {
          const waiting = pending; pending = null; clearTimeout(waiting.timer); waiting.resolve(value);
        }
        next?.(value);
      });
      port.onDisconnect.addListener(() => { void runtime.lastError; close(); });
      return close;
    },
    readSnapshot() {
      if (closed || pending) return Promise.reject(new Error('Onboarding snapshot unavailable'));
      return new Promise((resolve, reject) => {
        pending = { resolve, reject: () => reject(new Error('Onboarding snapshot unavailable')),
          timer: setTimeout(close, DEADLINE_MS) };
        if (negotiated) { try { send({ type: 'snapshot' }); } catch { close(); } }
      });
    },
    sendCommand(command: OnboardingCommand, epoch: string) {
      if (!negotiated || command.owner !== 'extension') throw new Error('Onboarding capability unavailable');
      send({ type: 'command', epoch, command });
    },
    readOperation(operationId) {
      if (!negotiated) throw new Error('Onboarding capability unavailable');
      send({ type: 'operation', operation_id: operationId });
    },
    focusWorkspace() {
      if (!negotiated) throw new Error('Onboarding capability unavailable');
      send({ type: 'focus' });
    },
    invalidate: close,
  };
}
