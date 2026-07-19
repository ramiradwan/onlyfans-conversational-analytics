import { Box } from '@mui/material';
import type { ConversationMessageState } from '../../src/store/transportStore';
import {
  MessageStreamPane,
  previewConversations,
} from 'onlyfans-analytics-frontend';

const noOp = () => {};
const messageState: ConversationMessageState = {
  items: previewConversations[0].messages,
  olderCursor: null,
  hasOlderStoredItems: false,
  hasNewerUncachedItems: false,
  conversationCoverage: {
    status: 'complete',
    boundary: 'history_start',
    earliest_available_at: '2026-07-12T09:58:00.000Z',
    latest_acquired_at: '2026-07-18T10:00:00.000Z',
    data_as_of: '2026-07-18T12:00:00.000Z',
    reason_code: null,
  },
  projection: null,
  projectionGeneration: null,
  readRevision: 7,
  generatedAt: '2026-07-18T12:00:00.000Z',
  status: 'ready',
  error: null,
};

export function ActiveConversation() {
  return (
    <Box sx={{ height: 480, maxWidth: 760 }}>
      <MessageStreamPane
        conversation={previewConversations[0]}
        isLoading={false}
        isOnline
        messageState={messageState}
        onLoadOlder={noOp}
        onReloadLatest={noOp}
      />
    </Box>
  );
}

export function NoSelection() {
  return (
    <Box sx={{ height: 320, maxWidth: 760 }}>
      <MessageStreamPane
        conversation={null}
        isLoading={false}
        isOnline={false}
        messageState={null}
        onLoadOlder={noOp}
        onReloadLatest={noOp}
      />
    </Box>
  );
}

export function Loading() {
  return (
    <Box sx={{ height: 380, maxWidth: 760 }}>
      <MessageStreamPane
        conversation={null}
        isLoading
        isOnline={false}
        messageState={null}
        onLoadOlder={noOp}
        onReloadLatest={noOp}
      />
    </Box>
  );
}
