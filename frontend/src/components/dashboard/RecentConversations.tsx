import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import {
  Avatar,
  Box,
  Button,
  List,
  ListItem,
  ListItemAvatar,
  ListItemText,
  Paper,
  Stack,
  Typography,
} from '@mui/material';
import { useId } from 'react';
import { Link as RouterLink } from 'react-router-dom';

import type { ConversationSummary } from '../../protocol';
import { conversationLatestMessage } from '../../store/transportStore';
import {
  formatTimestamp,
  getConversationTitle,
  sortConversations,
} from '../inbox/inboxModel';
import { VisuallyHidden } from '../ui';

export interface RecentConversationsProps {
  conversations: readonly ConversationSummary[];
  limit?: number;
  /** Shows the link to the inbox when the viewer may open it. */
  showInboxLink?: boolean;
}

export function RecentConversations({
  conversations,
  limit = 4,
  showInboxLink = true,
}: RecentConversationsProps) {
  const headingId = useId();
  const recent = sortConversations(conversations).slice(0, limit);
  if (recent.length === 0) return null;

  return (
    <Paper
      component="section"
      aria-labelledby={headingId}
      sx={(theme) => ({ maxWidth: 880, mx: 'auto', py: 1, width: '100%', ...theme.effects.cardBorder(theme) })}
    >
      <Stack
        direction="row"
        spacing={2}
        sx={{ alignItems: 'center', justifyContent: 'space-between', pb: 0.5, pl: 3, pr: 2, pt: 1.5 }}
      >
        <Typography component="h2" id={headingId} variant="h6">
          Recent conversations
        </Typography>
        {showInboxLink && (
          <Button component={RouterLink} endIcon={<ArrowForwardIcon />} size="small" to="/inbox">
            Open inbox
          </Button>
        )}
      </Stack>
      <List disablePadding>
        {recent.map((conversation) => {
          const title = getConversationTitle(conversation);
          const latest = conversationLatestMessage(conversation);
          const lastActivity = conversation.last_message_at ?? latest?.sent_at ?? null;
          return (
            <ListItem key={conversation.conversation_id} sx={{ columnGap: 2, px: 3, py: 1.25 }}>
              <ListItemAvatar sx={{ minWidth: 0 }}>
                <Avatar
                  aria-hidden="true"
                  sx={{
                    bgcolor: 'surface.subtle',
                    color: 'text.primary',
                    fontSize: '0.95rem',
                    fontWeight: 600,
                    height: 36,
                    width: 36,
                  }}
                >
                  {title.slice(0, 1).toUpperCase()}
                </Avatar>
              </ListItemAvatar>
              <ListItemText
                primary={title}
                secondary={latest?.text.trim() || 'No messages yet'}
                slotProps={{
                  primary: { noWrap: true, sx: { fontWeight: 500 } },
                  secondary: { noWrap: true },
                }}
                sx={{ minWidth: 0 }}
              />
              <Stack spacing={0.5} sx={{ alignItems: 'flex-end', alignSelf: 'flex-start', flexShrink: 0, pt: 0.75 }}>
                {lastActivity !== null && (
                  <Typography
                    component="time"
                    dateTime={lastActivity}
                    variant="caption"
                    sx={{ color: 'text.secondary' }}
                  >
                    {formatTimestamp(lastActivity)}
                  </Typography>
                )}
                {conversation.unread_count > 0 ? (
                  <Box
                    component="span"
                    sx={{
                      bgcolor: 'primary.main',
                      borderRadius: 999,
                      color: 'primary.contrastText',
                      fontSize: '0.75rem',
                      fontVariantNumeric: 'tabular-nums',
                      fontWeight: 600,
                      lineHeight: '20px',
                      minWidth: 20,
                      px: 0.75,
                      textAlign: 'center',
                    }}
                  >
                    <span aria-hidden="true">{conversation.unread_count}</span>
                    <VisuallyHidden>
                      {conversation.unread_count === 1
                        ? '1 unread message'
                        : `${conversation.unread_count} unread messages`}
                    </VisuallyHidden>
                  </Box>
                ) : null}
              </Stack>
            </ListItem>
          );
        })}
      </List>
    </Paper>
  );
}
