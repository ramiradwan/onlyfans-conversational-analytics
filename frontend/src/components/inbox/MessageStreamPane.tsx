import {
  Alert,
  Box,
  Button,
  Chip,
  Paper,
  Skeleton,
  Stack,
  styled,
  Typography,
} from '@mui/material';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { getConversationTitle, sortMessages } from './inboxModel';
import { MessageBubble } from './MessageBubble';
import type { ConversationRecord } from '../../protocol';
import type { ConversationMessageState } from '../../store/transportStore';

export const MAX_RENDERED_MESSAGES = 160;
const WINDOW_SHIFT = 80;

const Pane = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  minHeight: 0,
  overflow: 'hidden',
}));

const PaneHeader = styled(Box)(({ theme }) => ({
  alignItems: 'center',
  backgroundColor: theme.vars.palette.background.paper,
  borderBottom: '1px solid ' + theme.vars.palette.divider,
  display: 'flex',
  justifyContent: 'space-between',
  minHeight: theme.spacing(8),
  padding: theme.spacing(1.5, 2),
}));

const Stream = styled(Box)(({ theme }) => ({
  flex: 1,
  minHeight: 0,
  overflowY: 'auto',
  padding: theme.spacing(2),
}));

const MessagesList = styled('ol')(({ theme }) => ({
  alignContent: 'start',
  display: 'grid',
  gap: theme.spacing(1.5),
  listStyle: 'none',
  margin: 0,
  minHeight: '100%',
  padding: 0,
}));

const CenteredState = styled(Box)(({ theme }) => ({
  alignItems: 'center',
  color: theme.vars.palette.text.secondary,
  display: 'flex',
  flexDirection: 'column',
  gap: theme.spacing(1),
  height: '100%',
  justifyContent: 'center',
  padding: theme.spacing(3),
  textAlign: 'center',
}));

const LiveStatus = styled('span')({
  clip: 'rect(0 0 0 0)',
  clipPath: 'inset(50%)',
  height: 1,
  overflow: 'hidden',
  position: 'absolute',
  whiteSpace: 'nowrap',
  width: 1,
});

interface MessageStreamPaneProps {
  conversation: ConversationRecord | null;
  isLoading: boolean;
  isOnline: boolean;
  messageState: ConversationMessageState | null;
  onLoadOlder(): void;
  onReloadLatest(): void;
}

interface PendingAnchor {
  messageId: string | null;
  pixelOffset: number;
}

export function MessageStreamPane({
  conversation,
  isLoading,
  isOnline,
  messageState,
  onLoadOlder,
  onReloadLatest,
}: MessageStreamPaneProps) {
  const streamRef = useRef<HTMLDivElement>(null);
  const endOfStream = useRef<HTMLLIElement>(null);
  const pendingAnchor = useRef<PendingAnchor | null>(null);
  const nearBottom = useRef(true);
  const previousLastMessageId = useRef<string | null>(null);
  const [windowStart, setWindowStart] = useState(0);
  const conversationId = conversation?.conversation_id ?? null;
  const messages = useMemo(
    () => sortMessages(messageState?.items ?? []),
    [messageState?.items],
  );
  const windowEnd = Math.min(messages.length, windowStart + MAX_RENDERED_MESSAGES);
  const renderedMessages = messages.slice(windowStart, windowEnd);

  useEffect(() => {
    setWindowStart(Math.max(0, messages.length - MAX_RENDERED_MESSAGES));
    pendingAnchor.current = null;
  }, [conversationId]);

  useLayoutEffect(() => {
    const anchor = pendingAnchor.current;
    const stream = streamRef.current;
    if (anchor === null || stream === null) return;
    const anchorIndex =
      anchor.messageId === null
        ? -1
        : messages.findIndex(({ message_id }) => message_id === anchor.messageId);
    const anchorElement = [...stream.querySelectorAll<HTMLElement>('[data-message-id]')].find(
      (element) => element.dataset.messageId === anchor.messageId,
    );
    if (anchor.messageId !== null && anchorElement === undefined && anchorIndex >= 0) {
      const nextStart = Math.max(0, anchorIndex - WINDOW_SHIFT);
      if (nextStart !== windowStart) setWindowStart(nextStart);
      return;
    }
    if (anchorElement) {
      const nextOffset = anchorElement.getBoundingClientRect().top - stream.getBoundingClientRect().top;
      stream.scrollTop += nextOffset - anchor.pixelOffset;
    }
    pendingAnchor.current = null;
  }, [messages, windowStart]);

  useLayoutEffect(() => {
    const lastMessageId = messages.at(-1)?.message_id ?? null;
    const appended =
      previousLastMessageId.current !== null &&
      previousLastMessageId.current !== lastMessageId;
    if (appended && pendingAnchor.current === null && nearBottom.current) {
      endOfStream.current?.scrollIntoView({ block: 'end' });
    }
    previousLastMessageId.current = lastMessageId;
  }, [messages]);

  useEffect(() => {
    if (isLoading || messages.length === 0 || messageState?.status === 'loading') return;
    const target = endOfStream.current;
    if (target === null || typeof target.scrollIntoView !== 'function') return;
    target.scrollIntoView({ block: 'end' });
  }, [conversationId, isLoading]);

  const captureAnchor = useCallback(() => {
    const stream = streamRef.current;
    const streamTop = stream?.getBoundingClientRect().top ?? 0;
    const visible = stream
      ? [...stream.querySelectorAll<HTMLElement>('[data-message-id]')].find(
          (element) => element.getBoundingClientRect().bottom > streamTop,
        )
      : undefined;
    pendingAnchor.current = {
      messageId: visible?.dataset.messageId ?? renderedMessages[0]?.message_id ?? null,
      pixelOffset: visible ? visible.getBoundingClientRect().top - streamTop : 0,
    };
  }, [renderedMessages]);

  const loadOlder = () => {
    captureAnchor();
    if (windowStart > 0) {
      setWindowStart(Math.max(0, windowStart - WINDOW_SHIFT));
      return;
    }
    onLoadOlder();
  };

  const handleScroll = () => {
    const stream = streamRef.current;
    if (stream === null) return;
    if (stream.scrollTop < 24 && windowStart > 0) {
      captureAnchor();
      setWindowStart(Math.max(0, windowStart - WINDOW_SHIFT));
      return;
    }
    const distanceFromBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
    nearBottom.current = distanceFromBottom < 48;
    if (distanceFromBottom < 24 && windowEnd < messages.length) {
      setWindowStart(
        Math.min(messages.length - MAX_RENDERED_MESSAGES, windowStart + WINDOW_SHIFT),
      );
    }
  };

  const title = conversation === null ? 'No conversation selected' : getConversationTitle(conversation);
  const loadingMessages =
    isLoading ||
    (conversation !== null &&
      (messageState === null || (messageState.status === 'loading' && messages.length === 0)));
  const canLoadOlder =
    windowStart > 0 ||
    (messageState?.hasOlderStoredItems === true && messageState.olderCursor !== null);

  return (
    <Pane variant="outlined" role="region" aria-labelledby="message-stream-title">
      <PaneHeader>
        <Box>
          <Typography id="message-stream-title" component="h2" variant="subtitle1">
            {isLoading ? 'Messages' : title}
          </Typography>
          {conversation !== null && !loadingMessages && (
            <Typography variant="caption" sx={{
              color: 'text.secondary'
            }}>
              {messages.length === 1 ? '1 message loaded' : `${messages.length} messages loaded`}
            </Typography>
          )}
        </Box>
        <Stack direction="row" spacing={1} sx={{
          alignItems: 'center'
        }}>
          {messageState?.hasNewerUncachedItems && (
            <Button
              size="small"
              disabled={messageState.status === 'loading'}
              onClick={onReloadLatest}
            >
              Return to latest
            </Button>
          )}
          {conversation !== null && isOnline && (
            <Chip label="Online" color="success" size="small" variant="outlined" />
          )}
        </Stack>
      </PaneHeader>

      <LiveStatus aria-atomic="true" aria-live="polite" role="status">
        {messageState?.status === 'loading'
          ? 'Loading message history.'
          : messageState?.status === 'error'
            ? 'Message history is unavailable.'
            : ''}
      </LiveStatus>
      <Stream data-message-scroll="true" ref={streamRef} onScroll={handleScroll}>
        {loadingMessages ? (
          <CenteredState role="status">
            <Typography variant="body2">Loading messages…</Typography>
            <Stack spacing={2} sx={{
              width: '100%'
            }}>
              <Skeleton variant="rounded" width="58%" height={72} />
              <Skeleton variant="rounded" width="62%" height={88} sx={{ alignSelf: 'flex-end' }} />
              <Skeleton variant="rounded" width="48%" height={72} />
            </Stack>
          </CenteredState>
        ) : conversation === null ? (
          <CenteredState>
            <Typography component="p" variant="body1">Select a conversation</Typography>
            <Typography component="p" variant="body2">
              Messages from the selected chat will appear here.
            </Typography>
          </CenteredState>
        ) : messageState?.status === 'error' && messages.length === 0 ? (
          <CenteredState>
            <Alert severity="warning">
              {messageState.error ?? 'Message history is temporarily unavailable.'}
            </Alert>
            <Button onClick={onReloadLatest}>Try again</Button>
          </CenteredState>
        ) : messages.length === 0 ? (
          <CenteredState>
            <Typography component="p" variant="body1">
              {messageState?.conversationCoverage?.status === 'complete'
                ? 'No messages in this conversation'
                : 'No stored messages yet'}
            </Typography>
          </CenteredState>
        ) : (
          <MessagesList aria-label={'Messages with ' + title}>
            <li>
                <Stack
                  spacing={1}
                  sx={{
                    alignItems: 'center',
                    pb: 1
                  }}>
                  <Button
                    disabled={!canLoadOlder || messageState?.status === 'loading'}
                    onClick={loadOlder}
                    size="small"
                    variant="outlined"
                  >
                    {messageState?.status === 'loading'
                      ? 'Loading earlier…'
                      : canLoadOlder
                        ? 'Load earlier messages'
                        : messageState?.conversationCoverage.boundary === 'history_start'
                          ? 'Start of available history'
                          : 'No earlier stored messages'}
                  </Button>
                  {messageState?.error && (
                    <Typography color="error" variant="caption">{messageState.error}</Typography>
                  )}
                </Stack>
              </li>
            {renderedMessages.map((message) => (
              <MessageBubble key={message.message_id} message={message} />
            ))}
            <li ref={endOfStream} aria-hidden="true" />
          </MessagesList>
        )}
      </Stream>
    </Pane>
  );
}
