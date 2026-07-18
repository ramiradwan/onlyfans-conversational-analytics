import type {
  AgentStatePayload,
  AnalyticsView,
  BridgeSessionPayload,
  ConversationView,
  PresenceStatePayload,
  ProtocolErrorPayload,
  StateDeltaPayload,
  StateSnapshotPayload,
  SystemStatePayload,
} from '../protocol';

export type BridgeConnectionState =
  | 'idle'
  | 'connecting'
  | 'handshaking'
  | 'connected'
  | 'reconnecting'
  | 'disconnected'
  | 'error';

export type ReadModelState = 'empty' | 'loading' | 'realtime' | 'resyncing' | 'degraded';

export interface BridgeTransportState {
  connection: BridgeConnectionState;
  creatorAccountId: string | null;
  session: BridgeSessionPayload | null;
  readModelState: ReadModelState;
  viewRevision: number | null;
  conversations: ConversationView[];
  analytics: AnalyticsView;
  presence: PresenceStatePayload | null;
  agent: AgentStatePayload | null;
  system: SystemStatePayload | null;
  protocolError: ProtocolErrorPayload | null;
}

export interface BridgeTransportStore {
  getState(): Readonly<BridgeTransportState>;
  subscribe(listener: () => void): () => void;
  bindAccount(accountId: string): void;
  setConnection(connection: BridgeConnectionState): void;
  acceptSession(session: BridgeSessionPayload): void;
  applySnapshot(snapshot: StateSnapshotPayload): boolean;
  applyDelta(delta: StateDeltaPayload): 'applied' | 'duplicate' | 'gap' | 'invalid';
  beginResync(): void;
  setPresence(presence: PresenceStatePayload): void;
  expirePresence(expectedExpiresAt: string): void;
  setAgent(agent: AgentStatePayload): void;
  setSystem(system: SystemStatePayload): void;
  setProtocolError(error: ProtocolErrorPayload): void;
  markDisconnected(): void;
  reset(): void;
}

const EMPTY_ANALYTICS: AnalyticsView = {
  total_conversations: 0,
  total_messages: 0,
  inbound_messages: 0,
  outbound_messages: 0,
};

function cloneConversation(conversation: ConversationView): ConversationView {
  return {
    ...conversation,
    messages: conversation.messages.map((message) => ({ ...message })),
  };
}

function validConversations(conversations: ConversationView[]): boolean {
  const conversationIds = new Set<string>();
  for (const conversation of conversations) {
    if (conversationIds.has(conversation.conversation_id)) return false;
    conversationIds.add(conversation.conversation_id);
    const messageIds = new Set<string>();
    for (const message of conversation.messages) {
      if (messageIds.has(message.message_id)) return false;
      messageIds.add(message.message_id);
    }
  }
  return true;
}

function initialState(): BridgeTransportState {
  return {
    connection: 'idle',
    creatorAccountId: null,
    session: null,
    readModelState: 'empty',
    viewRevision: null,
    conversations: [],
    analytics: { ...EMPTY_ANALYTICS },
    presence: null,
    agent: null,
    system: null,
    protocolError: null,
  };
}

export function createBridgeTransportStore(): BridgeTransportStore {
  let state = initialState();
  const listeners = new Set<() => void>();

  const publish = (patch: Partial<BridgeTransportState>) => {
    state = { ...state, ...patch };
    listeners.forEach((listener) => listener());
  };

  const assertAccount = (accountId: string) => {
    if (state.creatorAccountId !== accountId) {
      throw new Error('Message account conflicts with the active Bridge binding');
    }
  };

  return {
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    bindAccount(accountId) {
      if (state.creatorAccountId !== accountId) state = initialState();
      publish({
        creatorAccountId: accountId,
        connection: 'connecting',
        readModelState: 'loading',
      });
    },
    setConnection(connection) {
      publish({ connection });
    },
    acceptSession(session) {
      assertAccount(session.creator_account_id);
      publish({
        session,
        connection: 'connected',
        readModelState: 'loading',
        protocolError: null,
      });
    },
    applySnapshot(snapshot) {
      assertAccount(snapshot.creator_account_id);
      if (!validConversations(snapshot.conversations)) return false;
      publish({
        conversations: snapshot.conversations.map(cloneConversation),
        analytics: { ...snapshot.analytics },
        viewRevision: snapshot.view_revision,
        readModelState: 'realtime',
      });
      return true;
    },
    applyDelta(delta) {
      assertAccount(delta.creator_account_id);
      const currentRevision = state.viewRevision;
      if (currentRevision !== null && delta.view_revision <= currentRevision) return 'duplicate';
      if (
        currentRevision === null ||
        state.readModelState === 'resyncing' ||
        delta.view_revision !== currentRevision + 1
      ) return 'gap';

      const conversations = state.conversations.map(cloneConversation);
      let analytics = { ...state.analytics };
      const changedKeys = new Set<string>();
      for (const change of delta.changes) {
        if (change.type === 'analytics.replace') {
          if (changedKeys.has('analytics')) return 'invalid';
          changedKeys.add('analytics');
          analytics = { ...change.analytics };
          continue;
        }

        const conversationId =
          change.type === 'conversation.upsert'
            ? change.conversation.conversation_id
            : change.conversation_id;
        const key = `conversation:${conversationId}`;
        if (changedKeys.has(key)) return 'invalid';
        changedKeys.add(key);

        const index = conversations.findIndex(
          (conversation) => conversation.conversation_id === conversationId,
        );
        if (change.type === 'conversation.delete') {
          if (index === -1) return 'invalid';
          conversations.splice(index, 1);
          continue;
        }
        if (!validConversations([change.conversation])) return 'invalid';
        const replacement = cloneConversation(change.conversation);
        if (index === -1) conversations.push(replacement);
        else conversations[index] = replacement;
      }
      if (!validConversations(conversations)) return 'invalid';
      publish({
        conversations,
        analytics,
        viewRevision: delta.view_revision,
        readModelState: 'realtime',
      });
      return 'applied';
    },
    beginResync() {
      publish({ readModelState: 'resyncing' });
    },
    setPresence(presence) {
      assertAccount(presence.creator_account_id);
      publish({ presence: { ...presence } });
    },
    expirePresence(expectedExpiresAt) {
      if (
        state.presence?.freshness === 'current' &&
        state.presence.expires_at === expectedExpiresAt
      ) {
        publish({
          presence: {
            ...state.presence,
            freshness: 'unknown',
            online_platform_user_ids: [],
          },
        });
      }
    },
    setAgent(agent) {
      assertAccount(agent.creator_account_id);
      publish({ agent: { ...agent } });
    },
    setSystem(system) {
      assertAccount(system.creator_account_id);
      publish({ system: { ...system } });
    },
    setProtocolError(error) {
      publish({ protocolError: { ...error }, connection: error.fatal ? 'error' : state.connection });
    },
    markDisconnected() {
      publish({
        connection: 'disconnected',
        session: null,
        readModelState: state.viewRevision === null ? 'empty' : 'degraded',
        system: state.system
          ? {
              ...state.system,
              readiness: 'degraded',
              detail: 'Bridge connection to Brain is unavailable',
            }
          : null,
      });
    },
    reset() {
      state = initialState();
      listeners.forEach((listener) => listener());
    },
  };
}

export const bridgeTransportStore = createBridgeTransportStore();
