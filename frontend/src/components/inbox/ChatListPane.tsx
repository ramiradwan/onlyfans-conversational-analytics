import {
  Avatar,
  Badge,
  Box,
  List,
  ListItem,
  ListItemAvatar,
  ListItemButton,
  ListItemText,
  Paper,
  Skeleton,
  Stack,
  styled,
  Typography,
} from '@mui/material';

import { formatTimestamp, getConversationTitle, getLastMessage } from './inboxModel';
import { MessageFlagIcon } from './MessageFlagIcons';
import type { ConversationView } from '../../protocol';
import { componentTokens } from '../../theme';

const Pane = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  display: 'flex',
  flexDirection: 'column',
  height: '100%',
  minHeight: 0,
  minWidth: componentTokens.inbox.conversationPaneMinWidth,
  overflow: 'hidden',
}));

const PaneHeader = styled(Box)(({ theme }) => ({
  alignItems: 'baseline',
  borderBottom: '1px solid ' + theme.vars.palette.divider,
  display: 'flex',
  justifyContent: 'space-between',
  padding: theme.spacing(2),
}));

const ScrollArea = styled(Box)({
  minHeight: 0,
  overflowY: 'auto',
});

const ConversationItem = styled(ListItemButton)(({ theme }) => ({
  alignItems: 'flex-start',
  borderBottom: '1px solid ' + theme.vars.palette.divider,
  gap: theme.spacing(1),
  minHeight: theme.spacing(9),
  padding: theme.spacing(1.5, 2),
  '&.Mui-selected': {
    backgroundColor: theme.vars.palette.action.selected,
  },
  '&.Mui-selected:hover': {
    backgroundColor: theme.vars.palette.action.selected,
  },
  '&:hover': {
    backgroundColor: theme.vars.palette.action.hover,
  },
}));

const FanAvatar = styled(Avatar)(({ theme }) => ({
  backgroundColor: theme.vars.palette.calm.main,
  color: theme.vars.palette.calm.contrastText,
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

const LoadingState = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  padding: theme.spacing(2),
}));

const EmptyState = styled(Box)(({ theme }) => ({
  color: theme.vars.palette.text.secondary,
  padding: theme.spacing(4, 2),
  textAlign: 'center',
}));

interface ChatListPaneProps {
  conversations: readonly ConversationView[];
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
  return (
    <Pane variant="outlined" role="region" aria-labelledby="conversations-title">
      <PaneHeader>
        <Typography id="conversations-title" component="h2" variant="subtitle1">
          Conversations
        </Typography>
        {!isLoading && (
          <Typography variant="caption" color="text.secondary">
            {conversations.length}
          </Typography>
        )}
      </PaneHeader>

      <ScrollArea>
        {isLoading ? (
          <LoadingState role="status" aria-live="polite">
            <Typography variant="body2" color="text.secondary">
              Loading conversations…
            </Typography>
            {Array.from({ length: 4 }, (_, index) => (
              <Stack key={index} direction="row" spacing={2} alignItems="center">
                <Skeleton variant="circular" width={40} height={40} />
                <Box sx={{ flex: 1 }}>
                  <Skeleton variant="text" width="48%" />
                  <Skeleton variant="text" width="76%" />
                </Box>
              </Stack>
            ))}
          </LoadingState>
        ) : conversations.length === 0 ? (
          <EmptyState>
            <Typography component="p" variant="body1">
              No conversations yet
            </Typography>
            <Typography component="p" variant="body2">
              New chats will appear here when the read model updates.
            </Typography>
          </EmptyState>
        ) : (
          <List aria-label="Conversation list" disablePadding>
            {conversations.map((conversation) => {
              const lastMessage = getLastMessage(conversation.messages);
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
                    primary={
                      <Stack direction="row" spacing={1} justifyContent="space-between">
                        <Typography variant="body1" fontWeight={600} noWrap>
                          {title}
                        </Typography>
                        {lastActivity !== null && (
                          <Typography
                            component="time"
                            dateTime={lastActivity}
                            variant="caption"
                            color="text.secondary"
                            sx={{ flexShrink: 0 }}
                          >
                            {formatTimestamp(lastActivity)}
                          </Typography>
                        )}
                      </Stack>
                    }
                    secondary={
                      <PreviewRow>
                        <Typography variant="body2" noWrap sx={{ flex: 1, minWidth: 0 }}>
                          {lastMessage?.text.trim() || 'No messages yet'}
                        </Typography>
                        {lastMessage !== null && (
                          <MessageFlagIcon sentiment={lastMessage.sentiment} context="latest" />
                        )}
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
