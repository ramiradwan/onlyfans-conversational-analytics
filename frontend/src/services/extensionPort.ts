import { z } from 'zod';

import { getConfig } from '../config/fastapiConfig';

/**
 * Browser-local port to the Agent extension (ADR 0045).
 *
 * Chrome routes it by extension ID, so this page learns the extension's setup
 * stage as soon as it changes, without polling and without loopback traffic
 * before pairing. The port has no authority: it can ask the extension to open
 * one of its own pages, or start and cancel a pairing attempt this page owns.
 */
export const EXTENSION_PORT_NAME = 'ofca.desktop';
const EXTENSION_ID = /^[a-p]{32}$/;
const JOURNEY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const navigationSchema = z.strictObject({
  type: z.literal('navigate_setup'), version: z.literal(1),
  request_id: z.string().regex(JOURNEY_ID), journey_id: z.string().regex(JOURNEY_ID), expected_url: z.string(),
});

const stageSchema = z.enum([
  'unavailable', 'needs_terms', 'paused', 'needs_full', 'needs_site_access',
  'needs_account', 'ready_to_pair', 'pairing', 'paired',
]);
const attemptSchema = z.strictObject({
  state: z.enum(['pairing', 'compare', 'paired', 'failed', 'not_ready', 'cancelled']),
  comparison_code: z.string().regex(/^\d{6}$/).nullable(),
}).refine((value) => (value.state === 'compare') === (value.comparison_code !== null));
const messageSchema = z.strictObject({
  type: z.literal('state'),
  version: z.literal(1),
  stage: stageSchema,
  attempt: attemptSchema.nullable(),
});

export type ExtensionStage = z.infer<typeof stageSchema>;
export type ExtensionAttempt = z.infer<typeof attemptSchema>;
export type ExtensionStep = 'setup' | 'access' | 'history' | 'connection' | 'creator';

export interface ExtensionPortState {
  /** `absent`: no extension answers in this browser (another browser, or not installed). */
  status: 'connecting' | 'connected' | 'absent';
  stage: ExtensionStage | null;
  attempt: ExtensionAttempt | null;
}

interface PortLike {
  onMessage: { addListener(listener: (message: unknown) => void): void };
  onDisconnect: { addListener(listener: () => void): void };
  postMessage(message: unknown): void;
  disconnect(): void;
}

export interface RuntimeLike {
  connect(extensionId: string, options: { name: string }): PortLike;
  lastError?: unknown;
}

export interface ExtensionPort {
  getState(): ExtensionPortState;
  subscribe(listener: () => void): () => void;
  open(step: ExtensionStep): boolean;
  pair(): boolean;
  cancel(): boolean;
  /** Try again after the extension was absent, for example when the page regains focus. */
  retry(): void;
}

const ABSENT: ExtensionPortState = Object.freeze({ status: 'absent', stage: null, attempt: null });
const CONNECTING: ExtensionPortState = Object.freeze({ status: 'connecting', stage: null, attempt: null });

export function createExtensionPort({
  runtime,
  resolveRuntime = () => runtime,
  extensionId,
  location = globalThis.location,
}: {
  runtime?: RuntimeLike;
  resolveRuntime?: () => RuntimeLike | undefined;
  extensionId: string;
  location?: Pick<Location, 'href' | 'replace'>;
}): ExtensionPort {
  const listeners = new Set<() => void>();
  const usable = () => EXTENSION_ID.test(extensionId) && typeof resolveRuntime()?.connect === 'function';
  let state: ExtensionPortState = usable() ? CONNECTING : ABSENT;
  let port: PortLike | null = null;
  let pendingSetup = false, navigating = false;

  const publish = (next: ExtensionPortState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  // A port that answered and then dropped usually means the extension worker
  // went idle; reconnecting wakes it. A port that never answers is absent, and
  // is retried only on a page event, never on a timer.
  const connect = (retry: boolean) => {
    if (port !== null) return;
    const currentRuntime = resolveRuntime();
    if (!EXTENSION_ID.test(extensionId) || typeof currentRuntime?.connect !== 'function') {
      publish(ABSENT);
      return;
    }
    let delivered = false;
    let current: PortLike;
    try {
      current = currentRuntime.connect(extensionId, { name: EXTENSION_PORT_NAME });
    } catch {
      publish(ABSENT);
      return;
    }
    port = current;
    current.onMessage.addListener((message) => {
      if (port !== current) return;
      const navigation = navigationSchema.safeParse(message);
      if (navigation.success) {
        const request = navigation.data;
        if (!pendingSetup || navigating || !location
          || request.expected_url !== `http://bridge.localhost:17871/#journey=${request.journey_id}`
          || location.href !== request.expected_url) return;
        pendingSetup = false; navigating = true;
        location.replace(`chrome-extension://${extensionId}/setup.html#journey=${request.journey_id}`);
        return;
      }
      const parsed = messageSchema.safeParse(message);
      if (!parsed.success) return;
      delivered = true;
      publish({ status: 'connected', stage: parsed.data.stage, attempt: parsed.data.attempt });
    });
    current.onDisconnect.addListener(() => {
      void currentRuntime.lastError;
      if (port !== current) return;
      port = null;
      pendingSetup = false;
      if (navigating) return;
      if (listeners.size === 0) { publish(usable() ? CONNECTING : ABSENT); return; }
      if (delivered || retry) connect(false);
      else publish(ABSENT);
    });
  };

  const send = (message: object): boolean => {
    if (port === null || state.status !== 'connected') return false;
    try {
      port.postMessage(message);
      return true;
    } catch {
      return false;
    }
  };

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      connect(true);
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0 && port !== null) {
          const closing = port;
          port = null;
          state = usable() ? CONNECTING : ABSENT;
          try { closing.disconnect(); } catch { /* already closed */ }
        }
      };
    },
    open: (step) => {
      pendingSetup = ['setup', 'access'].includes(step);
      const sent = send({ type: 'open', version: 1, step });
      if (!sent) pendingSetup = false;
      return sent;
    },
    pair: () => send({ type: 'pair', version: 1 }),
    cancel: () => send({ type: 'cancel', version: 1 }),
    retry() {
      if (state.status !== 'absent' || listeners.size === 0) return;
      publish(CONNECTING);
      connect(true);
    },
  };
}

interface ChromeGlobal { chrome?: { runtime?: RuntimeLike } }

let defaultPort: ExtensionPort | null = null;

/** The page's single port, created on first use. */
export function defaultExtensionPort(): ExtensionPort {
  defaultPort ??= createExtensionPort({
    resolveRuntime: () => (globalThis as ChromeGlobal).chrome?.runtime,
    extensionId: getConfig().EXTENSION_ID,
  });
  return defaultPort;
}
