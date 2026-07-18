import {
  parseBrainToBridgeMessage,
  parseBridgeToBrainMessage,
  type BrainToBridgeMessage,
  type BridgeSessionMessage,
  type BridgeToBrainMessage,
  type PresenceStateMessage,
  type ProtocolErrorMessage,
  type StateDeltaMessage,
} from '../protocol';
import {
  bridgeTransportStore,
  type BridgeTransportStore,
} from '../store/transportStore';

const OPEN = 1;
const DEV_AUTH_TICKET = 'bridge-clean-dev-ticket-v1';
const DEV_ACCOUNT_ID = 'dev-creator-account';
const DEFAULT_URL = 'ws://localhost:8000/ws/bridge';

interface MessageEventLike {
  data: unknown;
}

export interface WebSocketLike {
  readyState: number;
  onopen: (() => void) | null;
  onmessage: ((event: MessageEventLike) => void) | null;
  onerror: (() => void) | null;
  onclose: (() => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

interface Scheduler {
  setTimeout(handler: () => void, delay: number): ReturnType<typeof setTimeout>;
  clearTimeout(handle: ReturnType<typeof setTimeout>): void;
}

export interface BridgeWebSocketOptions {
  url?: string;
  creatorAccountId?: string;
  authTicket?: string;
  bridgeSessionId?: string;
  clientVersion?: string;
  webSocketFactory?: (url: string) => WebSocketLike;
  store?: BridgeTransportStore;
  scheduler?: Scheduler;
  random?: () => number;
  idFactory?: () => string;
  now?: () => number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
}

const defaultScheduler: Scheduler = {
  setTimeout: (handler, delay) => setTimeout(handler, delay),
  clearTimeout: (handle) => clearTimeout(handle),
};

function defaultIdFactory(): string {
  return crypto.randomUUID();
}

function normalizeUrl(url: string): string {
  return url
    .replace(/\/api\/ws\/frontend\/[^/]+$/, '/ws/bridge')
    .replace(/\/api\/ws\/bridge$/, '/ws/bridge');
}

export class BridgeWebSocketService {
  private socket: WebSocketLike | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private presenceTimer: ReturnType<typeof setTimeout> | null = null;
  private url: string;
  private creatorAccountId: string;
  private readonly authTicket: string;
  private readonly bridgeSessionId: string;
  private readonly clientVersion: string;
  private readonly webSocketFactory: (url: string) => WebSocketLike;
  private readonly store: BridgeTransportStore;
  private readonly scheduler: Scheduler;
  private readonly random: () => number;
  private readonly idFactory: () => string;
  private readonly now: () => number;
  private readonly reconnectBaseMs: number;
  private readonly reconnectMaxMs: number;
  private reconnectAttempt = 0;
  private manuallyStopped = true;
  private reconnectAllowed = true;

  constructor(options: BridgeWebSocketOptions = {}) {
    this.url = normalizeUrl(options.url ?? DEFAULT_URL);
    this.creatorAccountId = options.creatorAccountId ?? DEV_ACCOUNT_ID;
    this.authTicket = options.authTicket ?? DEV_AUTH_TICKET;
    this.bridgeSessionId = options.bridgeSessionId ?? (options.idFactory ?? defaultIdFactory)();
    this.clientVersion = options.clientVersion ?? '0.7.1';
    this.webSocketFactory =
      options.webSocketFactory ?? ((url) => new WebSocket(url) as unknown as WebSocketLike);
    this.store = options.store ?? bridgeTransportStore;
    this.scheduler = options.scheduler ?? defaultScheduler;
    this.random = options.random ?? Math.random;
    this.idFactory = options.idFactory ?? defaultIdFactory;
    this.now = options.now ?? Date.now;
    this.reconnectBaseMs = options.reconnectBaseMs ?? 500;
    this.reconnectMaxMs = options.reconnectMaxMs ?? 30_000;
  }

  connect(url = this.url, creatorAccountId = this.creatorAccountId): void {
    const normalizedUrl = normalizeUrl(url);
    const accountChanged = creatorAccountId !== this.creatorAccountId;
    if (accountChanged) this.disconnect();
    this.url = normalizedUrl;
    this.creatorAccountId = creatorAccountId;
    this.manuallyStopped = false;
    this.reconnectAllowed = true;
    if (this.socket?.readyState === OPEN || this.store.getState().connection === 'connecting') return;
    this.store.bindAccount(creatorAccountId);
    this.openSocket(false);
  }

  disconnect(): void {
    this.manuallyStopped = true;
    this.reconnectAllowed = false;
    this.clearReconnectTimer();
    this.clearPresenceTimer();
    const socket = this.socket;
    this.socket = null;
    if (socket) socket.close(1000, 'Bridge stopped');
    this.store.markDisconnected();
  }

  requestResync(reason: 'revision_gap' | 'invalid_delta' | 'reconnect' | 'manual'): boolean {
    const session = this.store.getState().session;
    if (!session || !this.socket || this.socket.readyState !== OPEN) return false;
    const message: BridgeToBrainMessage = {
      type: 'state.resync',
      protocol_version: '1',
      message_id: this.idFactory(),
      payload: {
        connection_id: session.connection_id,
        bridge_session_id: session.bridge_session_id,
        creator_account_id: session.creator_account_id,
        last_applied_view_revision: this.store.getState().viewRevision ?? 0,
        reason,
      },
    };
    const validated = parseBridgeToBrainMessage(message);
    this.store.beginResync();
    this.socket.send(JSON.stringify(validated));
    return true;
  }

  private openSocket(reconnecting: boolean): void {
    this.store.setConnection(reconnecting ? 'reconnecting' : 'connecting');
    const socket = this.webSocketFactory(this.url);
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.store.setConnection('handshaking');
      const hello: BridgeToBrainMessage = {
        type: 'bridge.hello',
        protocol_version: '1',
        message_id: this.idFactory(),
        payload: {
          auth_ticket: this.authTicket,
          bridge_session_id: this.bridgeSessionId,
          requested_creator_account_id: this.creatorAccountId,
          capabilities: ['state.snapshot', 'state.delta', 'presence.state'],
          client_version: this.clientVersion,
          last_view_revision: this.store.getState().viewRevision,
        },
      };
      socket.send(JSON.stringify(parseBridgeToBrainMessage(hello)));
    };
    socket.onmessage = (event) => this.handleRawMessage(socket, event.data);
    socket.onerror = () => {
      if (this.socket === socket) this.store.setConnection('error');
    };
    socket.onclose = () => this.handleClose(socket);
  }

  private handleRawMessage(socket: WebSocketLike, data: unknown): void {
    if (socket !== this.socket) return;
    let decoded: unknown;
    try {
      decoded = JSON.parse(String(data));
    } catch {
      this.store.setConnection('error');
      socket.close(1002, 'Malformed JSON from Brain');
      return;
    }

    let message: BrainToBridgeMessage;
    try {
      message = parseBrainToBridgeMessage(decoded);
    } catch {
      const invalidType =
        typeof decoded === 'object' &&
        decoded !== null &&
        (decoded as Record<string, unknown>).type;
      if (invalidType === 'state.delta' || invalidType === 'state.snapshot') {
        this.requestResync('invalid_delta');
      } else {
        this.store.setConnection('error');
        socket.close(1002, 'Invalid protocol frame from Brain');
      }
      return;
    }

    if (message.type === 'protocol.error') {
      this.handleProtocolError(message);
      return;
    }

    const session = this.store.getState().session;
    if (session === null) {
      if (message.type !== 'bridge.session') {
        this.reconnectAllowed = true;
        socket.close(1002, 'Expected bridge.session');
        return;
      }
      this.acceptSession(message);
      return;
    }

    if (message.type === 'bridge.session') {
      socket.close(1002, 'Duplicate bridge.session');
      return;
    }
    if (message.payload.creator_account_id !== session.creator_account_id) {
      this.reconnectAllowed = false;
      socket.close(1008, 'Account identity conflict');
      return;
    }
    this.dispatchBoundMessage(message);
  }

  private acceptSession(message: BridgeSessionMessage): void {
    const payload = message.payload;
    if (
      payload.bridge_session_id !== this.bridgeSessionId ||
      payload.creator_account_id !== this.creatorAccountId
    ) {
      this.reconnectAllowed = false;
      this.socket?.close(1008, 'Session identity conflict');
      return;
    }
    this.reconnectAttempt = 0;
    this.store.acceptSession(payload);
  }

  private dispatchBoundMessage(
    message: Exclude<BrainToBridgeMessage, BridgeSessionMessage | ProtocolErrorMessage>,
  ): void {
    switch (message.type) {
      case 'state.snapshot':
        if (!this.store.applySnapshot(message.payload)) {
          this.requestResync('invalid_delta');
        }
        return;
      case 'state.delta':
        this.handleDelta(message);
        return;
      case 'presence.state':
        this.handlePresence(message);
        return;
      case 'agent.state':
        this.store.setAgent(message.payload);
        return;
      case 'system.state':
        this.store.setSystem(message.payload);
        return;
      default: {
        const exhaustive: never = message;
        return exhaustive;
      }
    }
  }

  private handleDelta(message: StateDeltaMessage): void {
    const result = this.store.applyDelta(message.payload);
    if (result === 'gap') this.requestResync('revision_gap');
    else if (result === 'invalid') this.requestResync('invalid_delta');
  }

  private handlePresence(message: PresenceStateMessage): void {
    this.clearPresenceTimer();
    this.store.setPresence(message.payload);
    if (message.payload.freshness !== 'current' || message.payload.expires_at === null) return;
    const delay = Math.max(0, Date.parse(message.payload.expires_at) - this.now());
    const expectedExpiry = message.payload.expires_at;
    this.presenceTimer = this.scheduler.setTimeout(() => {
      this.presenceTimer = null;
      this.store.expirePresence(expectedExpiry);
    }, delay);
  }

  private handleProtocolError(message: ProtocolErrorMessage): void {
    this.store.setProtocolError(message.payload);
    if (message.payload.fatal) {
      this.reconnectAllowed = message.payload.retryable;
      this.socket?.close(1002, message.payload.code);
    } else if (message.payload.retryable && message.payload.code === 'validation_failed') {
      this.requestResync('invalid_delta');
    }
  }

  private handleClose(socket: WebSocketLike): void {
    if (this.socket !== socket) return;
    this.socket = null;
    this.clearPresenceTimer();
    this.store.markDisconnected();
    if (!this.manuallyStopped && this.reconnectAllowed) this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    const exponential = Math.min(
      this.reconnectMaxMs,
      this.reconnectBaseMs * 2 ** this.reconnectAttempt,
    );
    const jittered = Math.max(0, Math.round(exponential * (0.5 + this.random())));
    this.reconnectAttempt += 1;
    this.store.setConnection('reconnecting');
    this.reconnectTimer = this.scheduler.setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.manuallyStopped) this.openSocket(true);
    }, jittered);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      this.scheduler.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private clearPresenceTimer(): void {
    if (this.presenceTimer !== null) {
      this.scheduler.clearTimeout(this.presenceTimer);
      this.presenceTimer = null;
    }
  }
}

export const websocketService = new BridgeWebSocketService();
