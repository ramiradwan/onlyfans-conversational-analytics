import {
  Alert,
  AlertTitle,
  Box,
  styled,
  Typography,
} from '@mui/material';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';

import type {
  AnalyticsConversationInsight,
  AnalyticsReadState,
  AnalyticsWindowSource,
} from '../analytics';
import { ChatListPane } from '../components/inbox/ChatListPane';
import { ConversationInsightsPanel } from '../components/inbox/ConversationInsightsPanel';
import { getConversationTitle, sortConversations } from '../components/inbox/inboxModel';
import { MessageStreamPane } from '../components/inbox/MessageStreamPane';
import { SetupPrompt } from '../components/SetupPrompt';
import {
  messageApi as defaultMessageApi,
  MessageApiError,
  StaleMessageCursorError,
  type MessageApi,
} from '../services/messageApi';
import {
  bridgeTransportStore,
  type BridgeTransportStore,
  type BridgeTransportState,
} from '../store/transportStore';
import { coverageProgressLabel, humanizeProjectionReason } from '../utils/dataReadiness';
import {
  extensionConnection,
  extensionIssue,
  protocolErrorText,
  setupIncomplete,
  type StatusMessage,
} from '../utils/statusCopy';

const InboxRoot = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  display: 'flex',
  flex: 1,
  flexDirection: 'column',
  minHeight: 0,
}));

const InboxGrid = styled(Box, {
  shouldForwardProp: (property) => property !== '$withInsights',
})<{ $withInsights: boolean }>(({ theme, $withInsights }) => ({
  display: 'grid',
  flex: 1,
  gap: theme.spacing(2),
  gridTemplateColumns: 'minmax(0, 1fr)',
  gridTemplateRows: 'minmax(14rem, 36%) minmax(0, 1fr)',
  minHeight: 0,
  overflow: 'hidden',
  [theme.breakpoints.up('md')]: {
    gridTemplateColumns: $withInsights
      ? 'minmax(18rem, 0.85fr) minmax(0, 2fr) minmax(17rem, 0.85fr)'
      : 'minmax(18rem, 0.85fr) minmax(0, 2fr)',
    gridTemplateRows: 'minmax(0, 1fr)',
  },
}));

const StatusAlert = styled(Alert)(({ theme }) => ({
  marginBottom: theme.spacing(2),
}));

function getIssue(state: ReturnType<OperatorInboxStore['getState']>): StatusMessage | null {
  if (state.protocolError !== null) {
    return {
      detail: protocolErrorText(state.protocolError),
      severity: state.protocolError.fatal ? 'error' : 'warning',
      title: 'Connection problem',
    };
  }
  if (state.readModelState === 'resyncing') {
    return {
      detail:
        state.viewRevision === null
          ? 'Your conversations will appear in a moment.'
          : 'Showing your last conversations until the refresh finishes.',
      severity: 'info',
      title: 'Refreshing conversations',
    };
  }
  if (state.readModelState === 'degraded') {
    return state.viewRevision === null
      ? {
          detail: 'Your conversations will appear once the connection is back.',
          severity: 'warning',
          title: 'Conversations unavailable',
        }
      : {
          detail: 'Showing your last conversations while reconnecting.',
          severity: 'warning',
          title: 'Updates paused',
        };
  }
  if (
    state.viewRevision === null &&
    (state.connection === 'disconnected' || state.connection === 'error')
  ) {
    return {
      detail: 'Your conversations will appear once the connection is back.',
      severity: 'error',
      title: 'Conversations unavailable',
    };
  }
  if (state.system?.readiness === 'unavailable' || state.projection.status === 'unavailable') {
    return {
      detail: humanizeProjectionReason(
        state.projection.reason,
        "Your conversations can't be shown right now.",
      ),
      severity: 'error',
      title: 'Conversations unavailable',
    };
  }
  if (state.viewRevision === null || setupIncomplete(state.coverage)) return null;
  const connection = extensionConnection(state.agent);
  if (connection !== 'applying_settings') {
    const issue = extensionIssue(connection);
    if (issue !== null) return issue;
  }
  if (state.liveFreshness.status !== 'current') {
    return {
      detail: 'Your saved conversations are shown, but new messages may take longer to appear.',
      severity: 'warning',
      title: 'Updates delayed',
    };
  }
  if (state.connection === 'disconnected' || state.connection === 'error') {
    return {
      detail: 'Showing your last conversations while reconnecting.',
      severity: 'warning',
      title: 'Updates paused',
    };
  }
  if (state.coverage.status !== 'complete') {
    return {
      detail:
        state.coverage.phase === 'paused'
          ? 'Only messages synced so far are shown. You can resume history sync in Settings.'
          : `${coverageProgressLabel(state.coverage)}. Older messages may not be here yet.`,
      severity: 'info',
      title: state.coverage.phase === 'paused' ? 'History paused' : 'History still syncing',
    };
  }
  if (
    state.projection.status !== 'current' ||
    state.projection.projected_revision < state.projection.canonical_revision
  ) {
    return {
      detail: 'Your latest messages are being added.',
      severity: 'info',
      title: 'Updating conversations',
    };
  }
  return null;
}

type OperatorInboxState = Omit<
  Pick<
    BridgeTransportState,
    | 'agent'
    | 'connection'
    | 'conversations'
    | 'coverage'
    | 'liveFreshness'
    | 'messagePages'
    | 'presence'
    | 'projection'
    | 'protocolError'
    | 'readModelState'
    | 'session'
    | 'system'
    | 'viewRevision'
  >,
  'agent' | 'presence' | 'protocolError' | 'system'
> & {
  agent: Pick<
    NonNullable<BridgeTransportState['agent']>,
    | 'applied_config_revision'
    | 'applied_history_settings_revision'
    | 'connection_id'
    | 'degraded_reason'
    | 'required_config_revision'
    | 'required_history_settings_revision'
    | 'status'
  > | null;
  presence: Pick<
    NonNullable<BridgeTransportState['presence']>,
    'freshness' | 'online_platform_user_ids'
  > | null;
  protocolError: Pick<NonNullable<BridgeTransportState['protocolError']>, 'code' | 'fatal'> | null;
  system: Pick<NonNullable<BridgeTransportState['system']>, 'readiness'> | null;
};

interface OperatorInboxStore {
  getState(): Readonly<OperatorInboxState>;
  subscribe(listener: () => void): () => void;
  beginMessagePage: BridgeTransportStore['beginMessagePage'];
  applyMessagePage: BridgeTransportStore['applyMessagePage'];
  failMessagePage: BridgeTransportStore['failMessagePage'];
  clearMessagePage: BridgeTransportStore['clearMessagePage'];
  setActiveConversation?: BridgeTransportStore['setActiveConversation'];
}

interface OperatorInboxViewProps {
  messageApi?: MessageApi;
  store?: OperatorInboxStore;
  /**
   * Session-bound analytics read state (see `store/analyticsStore`). Omitted in
   * production today: protocol-v2's `ConversationSummary` carries no reference to the
   * opaque analytics identifier `AnalyticsUpdateDocument.conversation_metrics` uses, so
   * there is no honest way to resolve a canonical conversation to its analytics yet.
   * Passing this prop opts into rendering `ConversationInsightsPanel`.
   */
  analyticsState?: AnalyticsReadState;
  conversationInsight?: AnalyticsConversationInsight | null;
  analyticsWindowSource?: AnalyticsWindowSource;
}

export default function OperatorInboxView({
  messageApi = defaultMessageApi,
  store = bridgeTransportStore,
  analyticsState,
  conversationInsight = null,
  analyticsWindowSource,
}: OperatorInboxViewProps) {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const conversations = useMemo(
    () => sortConversations(state.conversations),
    [state.conversations],
  );
  const selectedConversation =
    conversations.find(
      (conversation) => conversation.conversation_id === selectedConversationId,
    ) ??
    conversations[0] ??
    null;
  const activeConversationId = selectedConversation?.conversation_id ?? null;
  const messageState =
    activeConversationId === null ? null : state.messagePages[activeConversationId] ?? null;
  const hasSnapshot = state.viewRevision !== null;
  const issue = getIssue(state);
  const showSetup = hasSnapshot && setupIncomplete(state.coverage) && conversations.length === 0;
  const isOnline =
    selectedConversation !== null &&
    selectedConversation.platform_user_id !== null &&
    state.presence?.freshness === 'current' &&
    state.presence.online_platform_user_ids.includes(
      selectedConversation.platform_user_id,
    );
  const initialPageRequest = useRef<{
    conversationId: string;
    controller: AbortController;
  } | null>(null);
  const lastReplacementAttemptKey = useRef<string | null>(null);
  const pageRecoveryKey =
    activeConversationId !== null &&
    state.viewRevision !== null &&
    state.readModelState === 'realtime' &&
    state.projection.status === 'current' &&
    state.projection.projected_revision >= state.projection.canonical_revision &&
    !['disconnected', 'error'].includes(state.connection)
      ? [
          activeConversationId,
          state.session?.connection_id ?? 'no-bridge-session',
          state.agent?.connection_id ?? 'no-agent-session',
          state.agent?.status ?? 'no-agent-status',
          state.projection.canonical_revision,
          state.projection.projected_revision,
          state.projection.projected_at ?? 'not-projected',
          state.viewRevision,
        ].join('|')
      : null;

  const loadPage = useCallback(
    async function loadConversationPage(
      mode: 'replace' | 'prepend',
      signal?: AbortSignal,
      retryStale = true,
    ): Promise<void> {
      if (activeConversationId === null) return;
      if (mode === 'replace' && pageRecoveryKey !== null) {
        lastReplacementAttemptKey.current = pageRecoveryKey;
      }
      const current = store.getState().messagePages[activeConversationId];
      const before = mode === 'prepend' ? current?.olderCursor ?? null : null;
      const projectionEpoch = store.beginMessagePage(
        activeConversationId,
        mode === 'replace',
      );
      try {
        const page = await messageApi.getPage({
          before,
          conversationId: activeConversationId,
          limit: 50,
          signal,
        });
        const result = store.applyMessagePage(page, mode, projectionEpoch);
        if (result === 'stale') throw new StaleMessageCursorError();
        if (result === 'invalid') throw new MessageApiError('Brain returned a mismatched message page.');
      } catch (error) {
        if (signal?.aborted) return;
        if (error instanceof StaleMessageCursorError) {
          store.clearMessagePage(activeConversationId);
          if (retryStale) {
            await loadConversationPage('replace', signal, false);
            return;
          }
          store.failMessagePage(
            activeConversationId,
            'Messages changed while loading. Try again.',
          );
          return;
        }
        store.failMessagePage(activeConversationId, "Messages couldn't load. Try again.");
      }
    },
    [activeConversationId, messageApi, pageRecoveryKey, store],
  );

  useEffect(() => {
    store.setActiveConversation?.(activeConversationId);
  }, [activeConversationId, store]);

  useEffect(() => {
    return () => {
      initialPageRequest.current?.controller.abort();
      initialPageRequest.current = null;
    };
  }, [activeConversationId]);

  useEffect(() => {
    if (
      activeConversationId === null ||
      messageState !== null ||
      initialPageRequest.current?.conversationId === activeConversationId
    ) return;
    const controller = new AbortController();
    initialPageRequest.current = { conversationId: activeConversationId, controller };
    void loadPage('replace', controller.signal).finally(() => {
      if (initialPageRequest.current?.controller === controller) {
        initialPageRequest.current = null;
      }
    });
  }, [activeConversationId, loadPage, messageState]);

  useEffect(() => {
    if (
      activeConversationId === null ||
      messageState?.status !== 'error' ||
      pageRecoveryKey === null ||
      lastReplacementAttemptKey.current === pageRecoveryKey
    ) return;

    lastReplacementAttemptKey.current = pageRecoveryKey;
    initialPageRequest.current?.controller.abort();
    const controller = new AbortController();
    initialPageRequest.current = { conversationId: activeConversationId, controller };
    void loadPage('replace', controller.signal, false).finally(() => {
      if (initialPageRequest.current?.controller === controller) {
        initialPageRequest.current = null;
      }
    });
  }, [activeConversationId, loadPage, messageState?.status, pageRecoveryKey]);

  return (
    <InboxRoot>
      <Typography component="h1" variant="h4" sx={{ mb: 2 }}>
        Inbox
      </Typography>

      {issue !== null && (
        <StatusAlert severity={issue.severity} role="alert">
          <AlertTitle>{issue.title}</AlertTitle>
          {issue.detail}
        </StatusAlert>
      )}

      {showSetup ? (
        <SetupPrompt
          extensionConnected={extensionConnection(state.agent) === 'connected'}
          title="Finish setup to see your conversations"
        />
      ) : (
      <InboxGrid aria-busy={!hasSnapshot} $withInsights={analyticsState !== undefined}>
        <ChatListPane
          conversations={conversations}
          isLoading={!hasSnapshot}
          onSelectConversation={setSelectedConversationId}
          selectedConversationId={activeConversationId}
        />
        <MessageStreamPane
          conversation={selectedConversation}
          isLoading={!hasSnapshot}
          isOnline={isOnline}
          messageState={messageState}
          onLoadOlder={() => void loadPage('prepend')}
          onReloadLatest={() => void loadPage('replace')}
        />
        {analyticsState !== undefined && (
          <ConversationInsightsPanel
            analyticsState={analyticsState}
            fanName={selectedConversation ? getConversationTitle(selectedConversation) : null}
            insight={conversationInsight}
            windowSource={analyticsWindowSource}
          />
        )}
      </InboxGrid>
      )}
    </InboxRoot>
  );
}
