import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { parseBridgeToBrainMessage } from '../src/protocol';
import {
  BridgeWebSocketService,
  type WebSocketLike,
} from '../src/services/websocketService';
import { createBridgeTransportStore } from '../src/store/transportStore';

const FIXTURES = resolve(process.cwd(), '../shared/fixtures/protocol/v1');
const BRIDGE_SESSION_ID = '60000000-0000-4000-8000-000000000001';

function fixture(name: string): Record<string, any> {
  return JSON.parse(readFileSync(resolve(FIXTURES, `${name}.json`), 'utf8'));
}

class MockSocket implements WebSocketLike {
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  sent: string[] = [];
  closeCode: number | undefined;

  send(data: string): void {
    this.sent.push(data);
  }

  close(code?: number): void {
    this.closeCode = code;
    this.readyState = 3;
    this.onclose?.();
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  receive(document: unknown): void {
    this.onmessage?.({ data: JSON.stringify(document) });
  }

  drop(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

function harness() {
  const sockets: MockSocket[] = [];
  const store = createBridgeTransportStore();
  const service = new BridgeWebSocketService({
    bridgeSessionId: BRIDGE_SESSION_ID,
    store,
    random: () => 0.5,
    idFactory: () => '90000000-0000-4000-8000-000000000001',
    webSocketFactory: () => {
      const socket = new MockSocket();
      sockets.push(socket);
      return socket;
    },
  });
  return { service, sockets, store };
}

function completeHandshake(socket: MockSocket, connectionId?: string): void {
  const session = fixture('bridge.session');
  if (connectionId) session.payload.connection_id = connectionId;
  socket.receive(session);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-07-18T10:04:01Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Bridge WebSocket lifecycle', () => {
  it('uses the golden handshake and dispatches all initial state slices', () => {
    const { service, sockets, store } = harness();
    service.connect();
    const socket = sockets[0];
    socket.open();

    const hello = parseBridgeToBrainMessage(JSON.parse(socket.sent[0]));
    expect(hello.type).toBe('bridge.hello');
    expect(hello.payload.auth_ticket).toBe('bridge-clean-dev-ticket-v1');
    expect(hello.payload.bridge_session_id).toBe(BRIDGE_SESSION_ID);

    completeHandshake(socket);
    socket.receive(fixture('state.snapshot'));
    socket.receive(fixture('presence.state'));
    socket.receive(fixture('agent.state'));
    socket.receive(fixture('system.state'));

    const state = store.getState();
    expect(state.connection).toBe('connected');
    expect(state.readModelState).toBe('realtime');
    expect(state.viewRevision).toBe(42);
    expect(state.presence?.freshness).toBe('current');
    expect(state.agent?.status).toBe('connected');
    expect(state.system?.readiness).toBe('ready');
  });

  it('applies only contiguous deltas and sends state.resync on a gap', () => {
    const { service, sockets, store } = harness();
    service.connect();
    const socket = sockets[0];
    socket.open();
    completeHandshake(socket);
    socket.receive(fixture('state.snapshot'));

    socket.receive(fixture('state.delta'));
    expect(store.getState().viewRevision).toBe(43);
    socket.receive(fixture('state.delta'));
    expect(store.getState().viewRevision).toBe(43);

    const gap = fixture('state.delta');
    gap.payload.view_revision = 45;
    socket.receive(gap);
    const resync = parseBridgeToBrainMessage(JSON.parse(socket.sent.at(-1)!));
    expect(resync.type).toBe('state.resync');
    expect(resync.payload.reason).toBe('revision_gap');
    expect(resync.payload.last_applied_view_revision).toBe(43);
    expect(store.getState().readModelState).toBe('resyncing');
  });

  it('turns the invalid state.snapshot fixture into resync without crashing', () => {
    const { service, sockets, store } = harness();
    service.connect();
    const socket = sockets[0];
    socket.open();
    completeHandshake(socket);
    socket.receive(fixture('state.snapshot'));

    const invalidSnapshot = fixture('invalid/wrong-type.state.snapshot');
    socket.receive(invalidSnapshot);

    const resync = parseBridgeToBrainMessage(JSON.parse(socket.sent.at(-1)!));
    expect(resync.type).toBe('state.resync');
    expect(resync.payload.reason).toBe('invalid_delta');
    expect(store.getState().readModelState).toBe('resyncing');
  });

  it('expires current presence to unknown locally at expires_at', () => {
    const { service, sockets, store } = harness();
    service.connect();
    const socket = sockets[0];
    socket.open();
    completeHandshake(socket);
    socket.receive(fixture('presence.state'));

    expect(store.getState().presence?.freshness).toBe('current');
    vi.advanceTimersByTime(120_000);
    expect(store.getState().presence?.freshness).toBe('unknown');
    expect(store.getState().presence?.online_platform_user_ids).toEqual([]);
    expect(store.getState().presence?.last_observation).not.toBeNull();
  });

  it('reconnects with backoff and repeats the hello/session handshake', () => {
    const { service, sockets, store } = harness();
    service.connect();
    sockets[0].open();
    completeHandshake(sockets[0]);
    sockets[0].receive(fixture('state.snapshot'));

    sockets[0].drop();
    expect(store.getState().connection).toBe('reconnecting');
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);
    sockets[1].open();
    expect(JSON.parse(sockets[1].sent[0]).type).toBe('bridge.hello');
    expect(JSON.parse(sockets[1].sent[0]).payload.last_view_revision).toBe(42);

    completeHandshake(sockets[1], '10000000-0000-4000-8000-000000000099');
    sockets[1].receive(fixture('state.snapshot'));
    expect(store.getState().connection).toBe('connected');
    expect(store.getState().session?.connection_id).toBe(
      '10000000-0000-4000-8000-000000000099',
    );
  });

  it('honors fatal non-retryable protocol.error without a reconnect storm', () => {
    const { service, sockets, store } = harness();
    service.connect();
    const socket = sockets[0];
    socket.open();
    const error = fixture('protocol.error');
    error.payload.fatal = true;
    error.payload.retryable = false;
    socket.receive(error);

    expect(socket.closeCode).toBe(1002);
    vi.runAllTimers();
    expect(sockets).toHaveLength(1);
    expect(store.getState().protocolError?.fatal).toBe(true);
  });
});
