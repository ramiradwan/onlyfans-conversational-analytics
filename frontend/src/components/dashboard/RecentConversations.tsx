import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import {
  Avatar,
  Box,
  Button,
  List,
  ListItem,
  ListItemAvatar,
  ListItemButton,
  Paper,
  Typography,
} from '@mui/material';
import { useId } from 'react';
import { Link as RouterLink } from 'react-router-dom';

import type { ConversationSummary } from '../../protocol';
import { conversationLatestMessage } from '../../store/transportStore';
import { componentTokens } from '../../theme';
import { surfaceArrival } from '../../theme/presentationMotion';
import { messagePreview } from '../../utils/sanitizeMessageHtml';
import {
  formatTimestamp,
  getConversationTitle,
  sortConversations,
} from '../inbox/inboxModel';
import { VisuallyHidden } from '../ui';
import { BoundedSummary } from '../ui/ReservedRegion';

const row = componentTokens.RecentConversations;
// Narrow rows put the time under the name so the name keeps its width.
const stacked = `@container (max-width: ${row.stackedRowMaxWidth})`;

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
  if (recent.length === 0) return <Box sx={{ p: 3 }}><Typography variant="subtitle1">No conversations yet</Typography><Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.5 }}>Conversations appear here when new messages arrive in your creator tab.</Typography></Box>;

  return (
    <Paper
      component="section"
      aria-labelledby={headingId}
      sx={(theme) => ({ width: '100%', overflow: 'hidden', ...theme.effects.cardBorder(theme), ...surfaceArrival(1, true) })}
    >
      <Box
        sx={{
          alignItems: 'center',
          borderBottom: '1px solid',
          borderColor: 'divider',
          columnGap: 2,
          display: 'flex',
          flexWrap: 'wrap',
          justifyContent: 'space-between',
          px: { xs: 2.5, md: 3 },
          py: 1.75,
        }}
      >
        <Typography component="h2" id={headingId} variant="h6">
          Recent conversations
        </Typography>
        {showInboxLink && (
          <Button component={RouterLink} endIcon={<ArrowForwardIcon />} size="small" sx={{ whiteSpace: 'nowrap', flexShrink: 0 }} to="/inbox">
            Open inbox
          </Button>
        )}
      </Box>
      <List disablePadding>
        {recent.map((conversation, index) => {
          const title = getConversationTitle(conversation);
          const latest = conversationLatestMessage(conversation);
          const lastActivity = conversation.last_message_at ?? latest?.sent_at ?? null;
          const stamp = lastActivity === null ? '' : formatTimestamp(lastActivity);
          // The date and the time wrap as whole parts.
          const split = stamp.lastIndexOf(', ');
          const rowContent = (
            <>
              <ListItemAvatar sx={{ minWidth: 0 }}>
                <Avatar
                  aria-hidden="true"
                  sx={{
                    bgcolor: 'surface.avatar.fill',
                    color: 'surface.avatar.text',
                    borderRadius: `${row.avatarRadius}px`,
                    fontSize: row.avatarFontSize,
                    fontWeight: 600,
                    height: row.avatarSize,
                    width: row.avatarSize,
                  }}
                >
                  {title.slice(0, 1).toUpperCase()}
                </Avatar>
              </ListItemAvatar>
              <Box sx={{ containerType: 'inline-size', flex: 1, minWidth: 0, my: 0.25 }}>
                <Box
                  sx={{
                    columnGap: 2,
                    display: 'grid',
                    gridTemplateAreas: '"name meta" "preview meta"',
                    gridTemplateColumns: `minmax(${row.nameMinWidth}, 1fr) auto`,
                    [stacked]: {
                      columnGap: 1,
                      gridTemplateAreas: '"name name" "stamp stamp" "preview badge"',
                      gridTemplateColumns: 'minmax(0, 1fr) auto',
                      gridTemplateRows: 'auto minmax(1.25rem, auto) auto',
                    },
                  }}
                >
                  <Typography component="span" sx={{ fontWeight: conversation.unread_count > 0 ? 600 : 500, gridArea: 'name', minWidth: 0, whiteSpace: 'nowrap' }}>
                    <BoundedSummary text={title} maximum={32} />
                  </Typography>
                  <Typography
                    variant="body2"
                    sx={{ color: 'text.secondary', gridArea: 'preview', lineHeight: 1.4, minWidth: 0, overflowWrap: 'anywhere', mt: 0.25 }}
                  >
                    <BoundedSummary text={messagePreview(latest?.text ?? '') || 'No messages yet'} maximum={84} />
                  </Typography>
                  <Box sx={{ alignItems: 'flex-end', display: 'flex', flexDirection: 'column', gap: 0.5, gridArea: 'meta', [stacked]: { display: 'contents' } }}>
                    {lastActivity !== null && (
                      <Typography
                        component="time"
                        dateTime={lastActivity}
                        variant="numericCaption"
                        sx={{ color: 'text.muted', gridArea: 'stamp', textAlign: 'end', '& > span': { whiteSpace: 'nowrap' }, [stacked]: { textAlign: 'start' } }}
                      >
                        {split < 0 ? stamp : <><span>{stamp.slice(0, split + 1)}</span> <span>{stamp.slice(split + 2)}</span></>}
                      </Typography>
                    )}
                    {conversation.unread_count > 0 ? (
                      <Box
                        component="span"
                        sx={{
                          bgcolor: 'measurement.main',
                          borderRadius: 999,
                          color: 'measurement.contrastText',
                          fontSize: '0.75rem',
                          fontVariantNumeric: 'tabular-nums',
                          fontWeight: 600,
                          gridArea: 'badge',
                          lineHeight: '1.25rem',
                          minWidth: '1.25rem',
                          px: 0.75,
                          textAlign: 'center',
                          [stacked]: { alignSelf: 'center' },
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
                  </Box>
                </Box>
              </Box>
            </>
          );

          return (
            <ListItem
              key={conversation.conversation_id}
              sx={{ borderTop: index > 0 ? '1px solid' : 'none', borderColor: 'divider', px: { xs: 2.5, md: 3 }, py: 0 }}
            >
              {showInboxLink ? (
                <ListItemButton
                  component={RouterLink}
                  to={`/inbox?conversation=${encodeURIComponent(conversation.conversation_id)}`}
                  sx={{
                    columnGap: 2, px: 0, py: 1.5, borderRadius: 0, position: 'static',
                    '&::before': { content: '""', position: 'absolute', inset: 0 },
                  }}
                >
                  {rowContent}
                </ListItemButton>
              ) : (
                <Box sx={{ display: 'flex', alignItems: 'center', width: '100%', columnGap: 2, py: 1.5 }}>
                  {rowContent}
                </Box>
              )}
            </ListItem>
          );
        })}
      </List>
    </Paper>
  );
}
