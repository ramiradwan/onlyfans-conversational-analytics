import {
  Alert,
  Box,
  Button,
  Paper,
  Stack,
  styled,
  Typography,
} from '@mui/material';
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import {
  formatMessageDateHeading,
  getConversationTitle,
  messageCalendarKey,
  sortMessages,
} from './inboxModel';
import { MessageBubble } from './MessageBubble';
import { PaneHeader, paneHeaderPaddingY } from './PaneHeader';
import type { ConversationRecord } from '../../protocol';
import type { ConversationMessageState } from '../../store/transportStore';
import { componentTokens } from '../../theme';
import { StatusChip } from '../ui';
import { LoadingCaption } from '../ui/Skeletons';

export const MAX_RENDERED_MESSAGES = 160;
const WINDOW_SHIFT = 80;

const Pane = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.default,
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  minHeight: 0,
  overflow: 'hidden',
  ...theme.effects.cardBorder(theme),
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
  margin: '0 auto',
  maxWidth: `calc(${componentTokens.inbox.bubbleMaxWidth} + ${theme.spacing(12)})`,
  minHeight: '100%',
  padding: 0,
  width: '100%',
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

const DateSeparator = styled('li')(({ theme }) => ({
  alignItems: 'center',
  color: theme.vars.palette.text.secondary,
  display: 'flex',
  gap: theme.spacing(1.5),
  padding: theme.spacing(0.75, 0),
  '&::before, &::after': {
    backgroundColor: theme.vars.palette.divider,
    content: '""',
    flex: 1,
    height: 1,
  },
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
  const loadOlderButton = useRef<HTMLButtonElement>(null);
  const historyBoundary = useRef<HTMLElement>(null);
  const restoreBoundaryFocus = useRef(false);
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
    if (messageState?.status === 'loading') return;
    restoreBoundaryFocus.current = document.activeElement === loadOlderButton.current;
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
  const showLoadOlder = canLoadOlder || messageState?.status === 'loading';

  // Keyboard focus moves to the history boundary when the button it was on goes away.
  useEffect(() => {
    if (showLoadOlder || !restoreBoundaryFocus.current) return;
    restoreBoundaryFocus.current = false;
    historyBoundary.current?.focus();
  }, [showLoadOlder]);

  return (
    <Pane data-reserved-region="message-stream" data-region-role="scroll" elevation={0} role="region" aria-labelledby="message-stream-title">
      <PaneHeader>
        {/* The first title line is centered. Longer titles run into the lower padding and scroll. */}
        <Box data-reading-viewport sx={(theme) => ({ minWidth: 0, flex: 1, height: `calc(${componentTokens.inbox.paneTitleHeight} + ${theme.spacing(paneHeaderPaddingY)})`, mb: -paneHeaderPaddingY, overflowY: 'auto', overflowWrap: 'anywhere' })}>
          <Typography id="message-stream-title" component="h2" variant="subtitle1" sx={{ py: `calc((${componentTokens.inbox.paneTitleHeight} - 1lh) / 2)` }}>
            {isLoading ? 'Messages' : title}
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
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
            <StatusChip label="Online" tone="success" size="small" />
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
      <Stream data-reading-viewport data-region-content sx={{ scrollbarGutter: 'stable' }} data-message-scroll="true" ref={streamRef} onScroll={handleScroll}>
        {loadingMessages ? (
          <MessagesList key="loading" role="status" aria-live="polite" aria-label="Loading messages" sx={{ pt: 2 }}>
            {[
              { outbound: false, width: '54%', opacity: 0.72 },
              { outbound: true, width: '46%', opacity: 0.36 },
            ].map((bubble, index) => (
              <Box
                key={index}
                component="li"
                sx={{
                  display: 'flex',
                  justifyContent: bubble.outbound ? 'flex-end' : 'flex-start',
                  opacity: bubble.opacity,
                }}
              >
                <Box
                  aria-hidden="true"
                  sx={(theme) => ({
                    bgcolor: bubble.outbound
                      ? theme.vars.palette.communication.outgoingSurface
                      : theme.vars.palette.communication.incomingSurface,
                    border: `1px solid ${bubble.outbound
                      ? theme.vars.palette.communication.outgoingBorder
                      : theme.vars.palette.communication.incomingBorder}`,
                    borderRadius: `${theme.shape.borderRadius}px`,
                    maxWidth: componentTokens.inbox.bubbleMaxWidth,
                    px: 2,
                    py: 2.25,
                    width: bubble.width,
                  })}
                >
                  <Box sx={{ bgcolor: 'placeholder', borderRadius: 1, height: 10, width: '78%' }} />
                </Box>
              </Box>
            ))}
            <Box component="li" sx={{ display: 'flex', justifyContent: 'center', pt: 1 }}>
              {conversation !== null ? (
                <LoadingCaption variant="caption">Loading messages…</LoadingCaption>
              ) : (
                <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                  Loading messages…
                </Typography>
              )}
            </Box>
          </MessagesList>
        ) : conversation === null ? (
          <CenteredState key="unselected">
            <Typography component="p" variant="body1">Select a conversation</Typography>
            <Typography component="p" variant="body2">
              Choose a conversation to read its messages.
            </Typography>
          </CenteredState>
        ) : messageState?.status === 'error' && messages.length === 0 ? (
          <CenteredState key="error" sx={{ gap: 2 }}>
            <Alert severity="warning" sx={{ minWidth: 0, overflowWrap: 'anywhere', maxHeight: '100%', overflowY: 'auto' }}>
              {messageState.error ?? "Messages couldn't load. Try again."}
            </Alert>
            <Button onClick={onReloadLatest} variant="outlined">Try again</Button>
          </CenteredState>
        ) : messages.length === 0 ? (
          <CenteredState key="empty">
            <Typography component="p" variant="body1">
              {messageState?.conversationCoverage?.status === 'complete'
                ? 'No messages in this conversation'
                : 'No messages saved yet'}
            </Typography>
          </CenteredState>
        ) : (
          <MessagesList aria-label={'Messages with ' + title}>
            <li>
              <Stack spacing={1} sx={{ alignItems: 'center', pb: 1 }}>
                {showLoadOlder ? (
                  <Button
                    aria-disabled={messageState?.status === 'loading'}
                    onClick={loadOlder}
                    ref={loadOlderButton}
                    size="small"
                    variant="outlined"
                  >
                    {messageState?.status === 'loading' ? 'Loading earlier…' : 'Load earlier messages'}
                  </Button>
                ) : (
                  <Typography
                    ref={historyBoundary}
                    tabIndex={-1}
                    variant="caption"
                    sx={{ color: 'text.secondary', '&:focus': { outline: 'none' } }}
                  >
                    {messageState?.conversationCoverage.boundary === 'history_start'
                      ? 'Start of conversation'
                      : 'No earlier messages on this computer'}
                  </Typography>
                )}
                {messageState?.error && (
                  <Typography color="error" variant="caption" sx={{ overflowWrap: 'anywhere', maxWidth: '100%' }}>{messageState.error}</Typography>
                )}
              </Stack>
            </li>
            {renderedMessages.map((message, index) => {
              const currentDay = messageCalendarKey(message.sent_at);
              const previousDay =
                index > 0 ? messageCalendarKey(renderedMessages[index - 1].sent_at) : null;
              const dateHeading =
                currentDay !== null && currentDay !== previousDay
                  ? formatMessageDateHeading(message.sent_at)
                  : '';

              return (
                <Fragment key={message.message_id}>
                  {dateHeading && (
                    <DateSeparator data-date-divider={currentDay ?? undefined}>
                      <Typography
                        component="span"
                        variant="caption"
                        sx={{
                          color: 'text.secondary',
                          fontWeight: 500,
                          letterSpacing: '0.02em',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {dateHeading}
                      </Typography>
                    </DateSeparator>
                  )}
                  <MessageBubble message={message} />
                </Fragment>
              );
            })}
            <li ref={endOfStream} aria-hidden="true" />
          </MessagesList>
        )}
      </Stream>
    </Pane>
  );
}
