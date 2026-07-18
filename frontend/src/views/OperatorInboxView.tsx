import CloudDoneIcon from '@mui/icons-material/CloudDone';
import CloudOffIcon from '@mui/icons-material/CloudOff';
import SyncIcon from '@mui/icons-material/Sync';
import {
  Alert,
  AlertTitle,
  Box,
  Chip,
  Stack,
  styled,
  Typography,
} from '@mui/material';
import { useMemo, useState, useSyncExternalStore } from 'react';

import { ChatListPane } from '../components/inbox/ChatListPane';
import { sortConversations } from '../components/inbox/inboxModel';
import { MessageStreamPane } from '../components/inbox/MessageStreamPane';
import {
  bridgeTransportStore,
  type BridgeTransportState,
  type BridgeTransportStore,
} from '../store/transportStore';

const InboxRoot = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  display: 'flex',
  flex: 1,
  flexDirection: 'column',
  minHeight: 0,
}));

const InboxHeader = styled(Stack)(({ theme }) => ({
  alignItems: 'center',
  flexDirection: 'row',
  justifyContent: 'space-between',
  marginBottom: theme.spacing(2),
}));

const InboxGrid = styled(Box)(({ theme }) => ({
  display: 'grid',
  flex: 1,
  gap: theme.spacing(2),
  gridTemplateColumns: 'minmax(0, 1fr)',
  gridTemplateRows: 'minmax(14rem, 36%) minmax(0, 1fr)',
  minHeight: 0,
  overflow: 'hidden',
  [theme.breakpoints.up('md')]: {
    gridTemplateColumns: 'minmax(18rem, 0.85fr) minmax(0, 2fr)',
    gridTemplateRows: 'minmax(0, 1fr)',
  },
}));

const StatusAlert = styled(Alert)(({ theme }) => ({
  marginBottom: theme.spacing(2),
}));

type IssuePresentation = {
  detail: string;
  severity: 'error' | 'warning';
  title: string;
};

function getIssue(state: Readonly<BridgeTransportState>): IssuePresentation | null {
  if (state.protocolError !== null) {
    return {
      detail: state.protocolError.detail,
      severity: state.protocolError.fatal ? 'error' : 'warning',
      title: 'Bridge communication error',
    };
  }
  if (state.readModelState === 'resyncing') {
    return {
      detail: 'Showing the latest complete snapshot while a fresh snapshot is requested.',
      severity: 'warning',
      title: 'Refreshing conversations',
    };
  }
  if (state.readModelState === 'degraded') {
    return {
      detail: state.system?.detail ?? 'Showing cached data until the Bridge reconnects.',
      severity: 'warning',
      title: 'Realtime updates paused',
    };
  }
  if (state.system?.readiness === 'unavailable') {
    return {
      detail: state.system.detail ?? 'The processing service is currently unavailable.',
      severity: 'error',
      title: 'Conversation data unavailable',
    };
  }
  if (state.system?.readiness === 'degraded') {
    return {
      detail: state.system.detail ?? 'Conversation processing is operating in a degraded state.',
      severity: 'warning',
      title: 'Conversation processing degraded',
    };
  }
  if (state.agent?.degraded_reason) {
    return {
      detail: state.agent.degraded_reason,
      severity: 'warning',
      title: 'Agent needs attention',
    };
  }
  if (state.agent?.status === 'stale' || state.agent?.status === 'disconnected') {
    return {
      detail: 'New platform activity may be delayed until the Agent reconnects.',
      severity: 'warning',
      title: state.agent.status === 'stale' ? 'Agent connection is stale' : 'Agent disconnected',
    };
  }
  if (
    state.viewRevision !== null &&
    (state.connection === 'disconnected' || state.connection === 'error')
  ) {
    return {
      detail: 'Showing the latest complete snapshot while the Bridge reconnects.',
      severity: 'warning',
      title: 'Realtime updates paused',
    };
  }
  return null;
}

interface OperatorInboxViewProps {
  store?: BridgeTransportStore;
}

export default function OperatorInboxView({
  store = bridgeTransportStore,
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
  const hasSnapshot = state.viewRevision !== null;
  const issue = getIssue(state);
  const isResyncing = state.readModelState === 'resyncing';
  const statusLabel = issue
    ? isResyncing
      ? 'Resyncing'
      : 'Degraded'
    : hasSnapshot && state.readModelState === 'realtime'
      ? 'Live'
      : 'Connecting';
  const statusIcon = issue ? (
    isResyncing ? (
      <SyncIcon />
    ) : (
      <CloudOffIcon />
    )
  ) : hasSnapshot ? (
    <CloudDoneIcon />
  ) : (
    <SyncIcon />
  );
  const isOnline =
    selectedConversation !== null &&
    state.presence?.freshness === 'current' &&
    state.presence.online_platform_user_ids.includes(
      selectedConversation.platform_user_id,
    );

  return (
    <InboxRoot>
      <InboxHeader>
        <Box>
          <Typography component="h1" variant="h5">
            Inbox
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Conversation read model
          </Typography>
        </Box>
        <Chip
          icon={statusIcon}
          label={statusLabel}
          color={issue ? (issue.severity === 'error' ? 'error' : 'warning') : 'success'}
          variant="outlined"
          aria-live="polite"
        />
      </InboxHeader>

      {issue !== null && (
        <StatusAlert severity={issue.severity} role="alert">
          <AlertTitle>{issue.title}</AlertTitle>
          {issue.detail}
        </StatusAlert>
      )}

      <InboxGrid aria-busy={!hasSnapshot}>
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
        />
      </InboxGrid>
    </InboxRoot>
  );
}
