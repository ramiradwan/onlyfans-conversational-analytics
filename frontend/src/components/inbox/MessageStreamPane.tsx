import {
  Box,
  Chip,
  Paper,
  Skeleton,
  Stack,
  styled,
  Typography,
} from '@mui/material';
import { useEffect, useMemo, useRef } from 'react';

import { getConversationTitle, sortMessages } from './inboxModel';
import { MessageBubble } from './MessageBubble';
import type { ConversationView } from '../../protocol';

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

interface MessageStreamPaneProps {
  conversation: ConversationView | null;
  isLoading: boolean;
  isOnline: boolean;
}

export function MessageStreamPane({
  conversation,
  isLoading,
  isOnline,
}: MessageStreamPaneProps) {
  const endOfStream = useRef<HTMLLIElement>(null);
  const messages = useMemo(
    () => sortMessages(conversation?.messages ?? []),
    [conversation],
  );

  useEffect(() => {
    const target = endOfStream.current;
    if (target === null || typeof target.scrollIntoView !== 'function') return;
    const reducedMotion =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    target.scrollIntoView(
      reducedMotion ? undefined : { behavior: 'smooth', block: 'end' },
    );
  }, [messages]);

  const title = conversation === null ? 'No conversation selected' : getConversationTitle(conversation);

  return (
    <Pane variant="outlined" role="region" aria-labelledby="message-stream-title">
      <PaneHeader>
        <Box>
          <Typography id="message-stream-title" component="h2" variant="subtitle1">
            {isLoading ? 'Messages' : title}
          </Typography>
          {conversation !== null && (
            <Typography variant="caption" color="text.secondary">
              {conversation.messages.length === 1
                ? '1 message'
                : conversation.messages.length + ' messages'}
            </Typography>
          )}
        </Box>
        {conversation !== null && isOnline && (
          <Chip label="Online" color="success" size="small" variant="outlined" />
        )}
      </PaneHeader>

      <Stream aria-live="polite">
        {isLoading ? (
          <CenteredState role="status">
            <Typography variant="body2">Loading messages…</Typography>
            <Stack spacing={2} width="100%">
              <Skeleton variant="rounded" width="58%" height={72} />
              <Skeleton variant="rounded" width="62%" height={88} sx={{ alignSelf: 'flex-end' }} />
              <Skeleton variant="rounded" width="48%" height={72} />
            </Stack>
          </CenteredState>
        ) : conversation === null ? (
          <CenteredState>
            <Typography component="p" variant="body1">
              Select a conversation
            </Typography>
            <Typography component="p" variant="body2">
              Messages from the selected chat will appear here.
            </Typography>
          </CenteredState>
        ) : messages.length === 0 ? (
          <CenteredState>
            <Typography component="p" variant="body1">
              No messages in this conversation
            </Typography>
          </CenteredState>
        ) : (
          <MessagesList aria-label={'Messages with ' + title}>
            {messages.map((message) => (
              <MessageBubble key={message.message_id} message={message} />
            ))}
            <li ref={endOfStream} aria-hidden="true" />
          </MessagesList>
        )}
      </Stream>
    </Pane>
  );
}
