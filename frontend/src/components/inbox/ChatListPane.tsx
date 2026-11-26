import CloseRoundedIcon from '@mui/icons-material/CloseRounded';
import SearchRoundedIcon from '@mui/icons-material/SearchRounded';
import {
  Avatar,
  Badge,
  Box,
  Button,
  IconButton,
  InputBase,
  List,
  ListItem,
  ListItemAvatar,
  ListItemButton,
  ListItemText,
  Paper,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  styled,
  Typography,
} from '@mui/material';
import { useMemo, useState } from 'react';

import {
  filterConversations,
  formatTimestamp,
  getConversationTitle,
  type ConversationFilter,
} from './inboxModel';
import { PaneHeader } from './PaneHeader';
import type { ConversationRecord } from '../../protocol';
import { conversationLatestMessage } from '../../store/transportStore';
import { componentTokens, effectTokens } from '../../theme';
import { messagePreview } from '../../utils/sanitizeMessageHtml';
import { BoundedSummary, ReservedValue } from '../ui/ReservedRegion';
import { LoadingDot } from '../ui/Skeletons';

const Pane = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  minHeight: 0,
  minWidth: `min(100%, ${componentTokens.inbox.conversationPaneMinWidth})`,
  overflow: 'hidden',
  ...theme.effects.cardBorder(theme),
}));

const FilterBar = styled(Box)(({ theme }) => ({
  borderBottom: `1px solid ${theme.vars.palette.divider}`,
  display: 'grid',
  gap: theme.spacing(1),
  padding: theme.spacing(1.25, 2),
}));

const SearchFieldBox = styled(Box)(({ theme }) => ({
  alignItems: 'center',
  backgroundColor: theme.vars.palette.background.default,
  border: `1px solid ${theme.vars.palette.divider}`,
  borderRadius: `${componentTokens.segmentedControl.trackRadius}px`,
  color: theme.vars.palette.text.secondary,
  display: 'flex',
  gap: theme.spacing(0.75),
  minHeight: 36,
  padding: theme.spacing(0, 0.75, 0, 1.25),
  transition: `border-color ${effectTokens.motion.duration.fast} ${effectTokens.motion.easing.standard}`,
  '&:focus-within': {
    borderColor: theme.vars.palette.primary.main,
    color: theme.vars.palette.text.primary,
  },
}));

const ScrollArea = styled(Box)({
  minHeight: 0,
  flex: 1,
  overflowY: 'auto',
  scrollbarGutter: 'stable',
});

const ConversationItem = styled(ListItemButton)(({ theme }) => ({
  alignItems: 'flex-start',
  borderBottom: '1px solid ' + theme.vars.palette.divider,
  gap: theme.spacing(1),
  minHeight: theme.spacing(9),
  padding: theme.spacing(1.5, 2),
  position: 'relative',
  '&.Mui-selected': {
    backgroundColor: theme.vars.palette.action.selected,
  },
  '&.Mui-selected:hover': {
    backgroundColor: theme.vars.palette.action.selected,
  },
  '&.Mui-selected .MuiAvatar-root': {
    backgroundColor: theme.vars.palette.background.paper,
  },
  '&:hover': {
    backgroundColor: theme.vars.palette.action.hover,
  },
}));

const FanAvatar = styled(Avatar)(({ theme }) => ({
  backgroundColor: theme.vars.palette.action.selected,
  color: theme.vars.palette.text.primary,
  fontWeight: theme.typography.fontWeightMedium,
}));

const PreviewRow = styled(Stack)(({ theme }) => ({
  alignItems: 'center',
  color: theme.vars.palette.text.secondary,
  flexDirection: 'row',
  gap: theme.spacing(1),
  marginTop: theme.spacing(0.5),
  minWidth: 0,
}));

const EmptyState = styled(Box)(({ theme }) => ({
  alignItems: 'center',
  color: theme.vars.palette.text.secondary,
  display: 'flex',
  flexDirection: 'column',
  gap: theme.spacing(1.5),
  padding: theme.spacing(4, 2),
  textAlign: 'center',
}));

const QUIET_ROWS = [
  { name: '52%', preview: '78%', opacity: 0.78 },
  { name: '44%', preview: '66%', opacity: 0.52 },
  { name: '58%', preview: '72%', opacity: 0.3 },
  { name: '46%', preview: '56%', opacity: 0.14 },
] as const;

interface ChatListPaneProps {
  conversations: readonly ConversationRecord[];
  isLoading: boolean;
  onSelectConversation(conversationId: string): void;
  selectedConversationId: string | null;
}

export function ChatListPane({
  conversations,
  isLoading,
  onSelectConversation,
  selectedConversationId,
}: ChatListPaneProps) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<ConversationFilter>('all');

  const unreadTotal = useMemo(
    () => conversations.filter((conversation) => conversation.unread_count > 0).length,
    [conversations],
  );
  const filteredConversations = useMemo(
    () => filterConversations(conversations, { query, filter }),
    [conversations, query, filter],
  );
  const hasActiveFilters = query.trim().length > 0 || filter !== 'all';

  const clearFilters = () => {
    setQuery('');
    setFilter('all');
  };

  return (
    <Pane data-reserved-region="chat-list" data-region-role="scroll" elevation={0} role="region" aria-labelledby="conversations-title">
      {/* The title and count share a baseline in the vertically centered row. */}
      <PaneHeader sx={{ alignContent: 'center', alignItems: 'baseline', flexWrap: 'wrap' }}>
        <Typography id="conversations-title" component="h2" variant="subtitle1">
          Conversations
        </Typography>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          {isLoading && <LoadingDot />}
          <Typography
            variant="caption"
            sx={{ color: 'text.secondary', fontVariantNumeric: 'tabular-nums' }}
          >
            {isLoading ? (
              <ReservedValue id="chat-count" value={undefined} />
            ) : hasActiveFilters ? (
              `${filteredConversations.length} of ${conversations.length}`
            ) : (
              <ReservedValue id="chat-count" value={conversations.length} />
            )}
          </Typography>
        </Stack>
      </PaneHeader>

      {!isLoading && conversations.length > 0 && (
        <FilterBar>
          <SearchFieldBox>
            <SearchRoundedIcon sx={{ fontSize: 18, flexShrink: 0 }} aria-hidden="true" />
            <InputBase
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search fans or messages"
              inputProps={{ 'aria-label': 'Search conversations' }}
              sx={{
                color: 'text.primary',
                flex: 1,
                fontSize: '0.875rem',
                '& input::placeholder': {
                  color: 'text.secondary',
                  opacity: 1,
                },
              }}
            />
            {query.length > 0 && (
              <IconButton
                size="small"
                aria-label="Clear search"
                onClick={() => setQuery('')}
                sx={{ p: 0.25 }}
              >
                <CloseRoundedIcon sx={{ fontSize: 16 }} />
              </IconButton>
            )}
          </SearchFieldBox>
          <ToggleButtonGroup
            exclusive
            fullWidth
            size="small"
            value={filter}
            aria-label="Filter conversations"
            onChange={(_, next: ConversationFilter | null) => {
              if (next !== null) setFilter(next);
            }}
            sx={{
              '& .MuiToggleButton-root': {
                fontVariantNumeric: 'tabular-nums',
                py: 0.5,
              },
            }}
          >
            <ToggleButton disableRipple value="all">
              All ({conversations.length})
            </ToggleButton>
            <ToggleButton disableRipple value="unread">
              Unread ({unreadTotal})
            </ToggleButton>
          </ToggleButtonGroup>
        </FilterBar>
      )}

      <ScrollArea data-region-content data-reading-viewport>
        {isLoading ? (
          <Box role="status" aria-live="polite" aria-label="Loading conversations">
            {QUIET_ROWS.map((row, index) => (
              <Stack
                key={index}
                direction="row"
                spacing={2}
                sx={{
                  alignItems: 'center',
                  borderBottom: '1px solid',
                  borderColor: 'divider',
                  minHeight: 72,
                  opacity: row.opacity,
                  px: 2,
                  py: 1.5,
                }}
              >
                <Box
                  aria-hidden="true"
                  sx={{ bgcolor: 'placeholder', borderRadius: '50%', flexShrink: 0, height: 38, width: 38 }}
                />
                <Stack spacing={0.85} sx={{ flex: 1, minWidth: 0 }}>
                  <Box aria-hidden="true" sx={{ bgcolor: 'placeholder', borderRadius: 1, height: 11, width: row.name }} />
                  <Box aria-hidden="true" sx={{ bgcolor: 'placeholder', borderRadius: 1, height: 9, width: row.preview }} />
                </Stack>
              </Stack>
            ))}
          </Box>
        ) : conversations.length === 0 ? (
          <EmptyState>
            <Typography component="p" variant="body1">
              No conversations to show
            </Typography>
          </EmptyState>
        ) : filteredConversations.length === 0 ? (
          <EmptyState>
            <Typography component="p" variant="body2" sx={{ color: 'text.primary', fontWeight: 500 }}>
              No matching conversations
            </Typography>
            <Typography component="p" variant="caption" sx={{ color: 'text.secondary' }}>
              Try a different search term or switch back to all conversations.
            </Typography>
            <Button size="small" variant="outlined" onClick={clearFilters}>
              Clear filters
            </Button>
          </EmptyState>
        ) : (
          <List aria-label="Conversation list" disablePadding>
            {filteredConversations.map((conversation) => {
              const lastMessage = conversationLatestMessage(conversation);
              const title = getConversationTitle(conversation);
              const selected = selectedConversationId === conversation.conversation_id;
              const lastActivity = conversation.last_message_at ?? lastMessage?.sent_at ?? null;
              const unreadLabel =
                conversation.unread_count === 1
                  ? '1 unread message'
                  : conversation.unread_count + ' unread messages';

              return (
                <ListItem key={conversation.conversation_id} disablePadding>
                  <ConversationItem
                    selected={selected}
                    aria-current={selected ? 'true' : undefined}
                    aria-label={'Conversation with ' + title + ', ' + unreadLabel}
                    onClick={() => onSelectConversation(conversation.conversation_id)}
                  >
                    <ListItemAvatar>
                      <Badge
                        badgeContent={conversation.unread_count}
                        color="primary"
                        max={99}
                        overlap="circular"
                      >
                        <FanAvatar aria-hidden="true">
                          {title.slice(0, 1).toUpperCase()}
                        </FanAvatar>
                      </Badge>
                    </ListItemAvatar>
                    <ListItemText
                      disableTypography
                      sx={{ minWidth: 0 }}
                      primary={
                        <Box sx={{
                          alignItems: 'baseline',
                          columnGap: 1,
                          display: 'flex',
                          flexWrap: 'wrap',
                        }}>
                          <Typography variant="body1" sx={{ flex: '1 1 min(100%, 7rem)', minWidth: 0, height: '1.5rem', whiteSpace: 'nowrap',
                            fontWeight: selected ? 700 : 600
                          }}>
                            <BoundedSummary text={title} maximum={componentTokens.reserved.name.maxGraphemes} />
                          </Typography>
                          {lastActivity !== null && (
                            <Typography
                              component="time"
                              dateTime={lastActivity}
                              variant="caption"
                              sx={{
                                color: 'text.secondary',
                                fontVariantNumeric: 'tabular-nums',
                                flex: '0 0 auto',
                                marginInlineStart: 'auto',
                                maxWidth: '100%',
                                minWidth: 0,
                                overflowWrap: 'anywhere',
                                textAlign: 'end',
                              }}>
                              {formatTimestamp(lastActivity)}
                            </Typography>
                          )}
                        </Box>
                      }
                      secondary={
                        <PreviewRow>
                          <Typography data-reserved-region={`preview-${conversation.conversation_id}`} variant="body2" sx={{ flex: 1, minWidth: 0, height: '2.5rem', lineHeight: '1.25rem', overflowWrap: 'anywhere' }}>
                            <BoundedSummary text={messagePreview(lastMessage?.text ?? '') || 'No messages yet'} maximum={componentTokens.reserved.preview.maxGraphemes} />
                          </Typography>
                        </PreviewRow>
                      }
                    />
                  </ConversationItem>
                </ListItem>
              );
            })}
          </List>
        )}
      </ScrollArea>
    </Pane>
  );
}
