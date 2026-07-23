import { Paper, Stack, styled, Typography } from '@mui/material';

import { formatTimestamp } from './inboxModel';
import { MessageFlagIcon } from './MessageFlagIcons';
import type { MessageView } from '../../protocol';
import { componentTokens } from '../../theme';
import { sanitizeMessageHtml } from '../../utils/sanitizeMessageHtml';

const BubbleRow = styled('li', {
  shouldForwardProp: (property) => property !== 'messageDirection',
})<{ messageDirection: MessageView['direction'] }>(({ messageDirection, theme }) => ({
  display: 'flex',
  justifyContent: messageDirection === 'outbound' ? 'flex-end' : 'flex-start',
  paddingInline: theme.spacing(1),
  width: '100%',
}));

const BubbleSurface = styled(Paper, {
  shouldForwardProp: (property) => property !== 'messageDirection',
})<{ messageDirection: MessageView['direction'] }>(({ messageDirection, theme }) => ({
  backgroundColor:
    messageDirection === 'outbound'
      ? theme.vars.palette.communication.outgoingSurface
      : theme.vars.palette.communication.incomingSurface,
  border:
    '1px solid ' +
    (messageDirection === 'outbound'
      ? theme.vars.palette.communication.outgoingBorder
      : theme.vars.palette.communication.incomingBorder),
  borderRadius: componentTokens.inbox.bubbleRadius,
  color: theme.vars.palette.text.primary,
  maxWidth:
    'min(' +
    componentTokens.inbox.bubbleMaxWidth +
    ', ' +
    componentTokens.inbox.bubbleWidth +
    ')',
  overflowWrap: 'anywhere',
  padding: theme.spacing(1.5, 2),
}));

const BubbleMetadata = styled(Stack)(({ theme }) => ({
  alignItems: 'center',
  color: theme.vars.palette.text.secondary,
  flexDirection: 'row',
  gap: theme.spacing(1),
  justifyContent: 'flex-end',
  marginTop: theme.spacing(0.75),
}));

interface MessageBubbleProps {
  message: MessageView;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const directionLabel = message.direction === 'outbound' ? 'Outbound message' : 'Inbound message';

  return (
    <BubbleRow data-message-id={message.message_id} messageDirection={message.direction}>
      <BubbleSurface
        role="article"
        messageDirection={message.direction}
        aria-label={directionLabel}
      >
        <Typography
          component="p"
          variant="body1"
          sx={{
            margin: 0,
            whiteSpace: 'pre-wrap',
            '& p': { margin: 0 },
            '& p + p': { marginTop: '0.5em' },
            '& ul, & ol': { margin: '0.25em 0', paddingInlineStart: '1.25em' },
            '& a': {
              color: 'inherit',
              textDecoration: 'underline',
              wordBreak: 'break-word',
            },
          }}
          // Message text is untrusted platform content; sanitizeMessageHtml() runs
          // it through a strict allowlist before it ever reaches the DOM.
          dangerouslySetInnerHTML={{ __html: sanitizeMessageHtml(message.text) }}
        />
        <BubbleMetadata>
          <MessageFlagIcon sentiment={message.sentiment} />
          <Typography
            component="time"
            dateTime={message.sent_at}
            variant="caption"
            sx={{
              color: 'inherit'
            }}
          >
            {formatTimestamp(message.sent_at)}
          </Typography>
        </BubbleMetadata>
      </BubbleSurface>
    </BubbleRow>
  );
}
