// External imports (alphabetical)  
import SendIcon from '@mui/icons-material/Send';  
import {  
  Avatar,  
  Badge,  
  Box,  
  Chip,  
  Divider,  
  Grid,  
  IconButton,  
  List,  
  ListItemAvatar,  
  ListItemButton,  
  ListItemText,  
  Stack,  
  TextField,  
  Typography,  
  useTheme,  
} from '@mui/material';  
import React, { useEffect, useMemo, useRef, useState } from 'react';  
import { shallow } from 'zustand/shallow';  
  
// Internal imports (alphabetical)  
import { ExtendedConversationNode, Message } from '@/types/backend';  
import { MessageBubble } from '@components/MessageBubble';  
import {  
  ChatListPlaceholder,  
  Fan360Placeholder,  
  MessageStreamPlaceholder,  
} from '@components/placeholders';  
import { Panel, AsyncContent } from '@components/ui';  
import { useAnalyticsStore } from '@store/analyticsStore';  
import { useChatStore } from '@store/chatStore';  
import { useEnrichmentStore, EnrichmentStoreState } from '@store/enrichmentStore';  
  
// Local type  
type EqualityFn<T> = (a: T, b: T) => boolean;  

// Helper to get the last message from a message array  
const getLastMessage = (messages: Message[] | undefined): Message | null =>  
  !messages || messages.length === 0 ? null : messages[messages.length - 1];  
  
/* ---------- SUBCOMPONENTS ---------- */  
function ChatListPane({  
  conversations,  
  selectedConvoId,  
  onSelectConvo,  
  unreadCounts,  
  messagesByConversation,  
  isLoading,  
}: {  
  conversations: ExtendedConversationNode[];  
  selectedConvoId: string | null;  
  onSelectConvo: (id: string) => void;  
  unreadCounts: Record<string, number>;  
  messagesByConversation: Record<string, Message[]>;  
  isLoading: boolean;  
}) {  
  const theme = useTheme();  
  return (  
    <Panel  
      sx={{  
        height: '100%',  
        overflowY: 'auto',  
        borderRight: `1px solid ${theme.vars.palette.divider}`,  
      }}  
    >  
      <AsyncContent<ExtendedConversationNode>  
        isLoading={isLoading}  
        data={conversations}  
        placeholder={<ChatListPlaceholder />}  
        emptyMessage={<ChatListPlaceholder rows={6} />}  
        render={(data) => (  
          <List aria-label="Conversation list">  
            {data.map((convo) => {  
              const lastMessage = getLastMessage(  
                messagesByConversation[convo.conversationId]  
              );  
              return (  
                <ListItemButton  
                  key={convo.conversationId}  
                  selected={selectedConvoId === convo.conversationId}  
                  onClick={() => onSelectConvo(convo.conversationId)}  
                  aria-label={`Conversation with ${  
                    convo.withUser?.displayName || 'Unknown User'  
                  }`}  
                  sx={{  
                    '&.Mui-selected': {  
                      backgroundColor: theme.vars.palette.action.selected,  
                    },  
                  }}  
                >  
                  <ListItemAvatar>  
                    <Badge  
                      badgeContent={unreadCounts[convo.conversationId] || 0}  
                      sx={{  
                        '& .MuiBadge-badge': {  
                          backgroundColor:  
                            theme.vars.palette.brand.optimisticAccentPrimary,  
                          color: theme.vars.palette.text.primary,  
                        },  
                      }}  
                    >  
                      <Avatar  
                        src={convo.withUser?.avatarThumbs?.c50 || ''}  
                        alt=""  
                      />  
                    </Badge>  
                  </ListItemAvatar>  
                  <ListItemText  
                    primary={convo.withUser?.displayName || 'Unknown User'}  
                    secondary={lastMessage?.text}  
                    primaryTypographyProps={{ noWrap: true }}  
                    secondaryTypographyProps={{  
                      noWrap: true,  
                      color: theme.vars.palette.text.secondary,  
                    }}  
                  />  
                </ListItemButton>  
              );  
            })}  
          </List>  
        )}  
      />  
    </Panel>  
  );  
}  
  
function MessageStreamPane({  
  messages,  
  isLoading,  
  onSend,  
}: {  
  messages: Message[];  
  isLoading: boolean;  
  onSend: (text: string) => void;  
}) {  
  const theme = useTheme();  
  const scrollRef = useRef<HTMLDivElement>(null);  
  const [draftMessage, setDraftMessage] = useState('');  
  
  useEffect(() => {  
    if (scrollRef.current) {  
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {  
        scrollRef.current.scrollIntoView();  
      } else {  
        scrollRef.current.scrollIntoView({ behavior: 'smooth' });  
      }  
    }  
  }, [messages]);  
  
  const handleSend = () => {  
    if (draftMessage.trim()) {  
      onSend(draftMessage.trim());  
      setDraftMessage('');  
    }  
  };  
  
  return (  
    <Panel sx={{ height: '100%', display: 'flex', flexDirection: 'column' }}>  
      <Box sx={{ flexGrow: 1, overflowY: 'auto' }}>  
        <AsyncContent<Message>  
          isLoading={isLoading}  
          data={messages}  
          placeholder={<MessageStreamPlaceholder />}  
          emptyMessage={<MessageStreamPlaceholder bubbles={4} />}  
          render={(data) =>  
            data.map((msg) => <MessageBubble key={msg.id} message={msg} />)  
          }  
        />  
        <div ref={scrollRef} />  
      </Box>  
      <Divider />  
      <Box sx={{ p: 2 }}>  
        <Stack direction="row" spacing={1}>  
          <TextField  
            fullWidth  
            variant="outlined"  
            placeholder="Type a message..."  
            size="small"  
            value={draftMessage}  
            onChange={(e) => setDraftMessage(e.target.value)}  
            onKeyDown={(e) => {  
              if (e.key === 'Enter' && !e.shiftKey) {  
                e.preventDefault();  
                handleSend();  
              }  
            }}  
          />  
          <IconButton  
            sx={{  
              backgroundColor: theme.vars.palette.brand.calmClearPrimary,  
              color: theme.vars.palette.brand.calmClearEtherealBlue,  
              '&:hover': {  
                backgroundColor:  
                  theme.vars.palette.brand.optimisticAccentPrimary,  
              },  
            }}  
            onClick={handleSend}  
            disabled={!draftMessage.trim()}  
          >  
            <SendIcon />  
          </IconButton>  
        </Stack>  
      </Box>  
    </Panel>  
  );  
}  
  
function Fan360InsightsPane({  
  convoId,  
  isLoading,  
}: {  
  convoId: string | null;  
  isLoading: boolean;  
}) {  
  const theme = useTheme();  
  
  // Explicitly typed hook to allow equalityFn  
  const useEnrichmentStoreTyped = useEnrichmentStore as <T>(  
    selector: (state: EnrichmentStoreState) => T,  
    equalityFn?: EqualityFn<T>  
  ) => T;  
  
  const enrichment = useEnrichmentStoreTyped(  
    (state) => (convoId ? state.enrichmentsByConversation[convoId] : null),  
    shallow  
  ) as ExtendedConversationNode | null;  
  
  const enrichmentData: ExtendedConversationNode[] =  
    convoId && enrichment ? [enrichment] : [];  
  
  return (  
    <Panel sx={{ height: '100%', overflowY: 'auto', p: 3 }}>  
      <Typography variant="h6" gutterBottom>  
        Fan360 Insights  
      </Typography>  
      <AsyncContent<ExtendedConversationNode>  
        isLoading={isLoading}  
        data={enrichmentData}  
        placeholder={<Fan360Placeholder />}  
        emptyMessage={<Fan360Placeholder />}  
        render={(data) => {  
          const e = data[0];  
          return (  
            <Stack spacing={2}>  
              <Typography variant="subtitle2">Sentiment</Typography>  
              {typeof e.sentiment === 'number' && (  
                <Chip  
                  label={`${Math.round(e.sentiment * 100)}% Positive`}  
                  sx={{  
                    backgroundColor:  
                      e.sentiment > 0.5  
                        ? theme.vars.palette.success.main  
                        : theme.vars.palette.error.main,  
                    color: theme.vars.palette.success.contrastText,  
                  }}  
                />  
              )}  
              <Typography variant="subtitle2">Topics</Typography>  
              <Stack direction="row" spacing={1} useFlexGap flexWrap="wrap">  
                {(e.topics ?? []).map((topic) => (  
                  <Chip key={topic.topicId} label={topic.description} />  
                ))}  
              </Stack>  
              <Typography variant="subtitle2">Suggested Actions</Typography>  
              <Stack spacing={1}>  
                {(e.actions ?? []).map((action) => (  
                  <Chip  
                    key={action.actionId}  
                    label={action.name}  
                    color="primary"  
                    variant="outlined"  
                  />  
                ))}  
              </Stack>  
            </Stack>  
          );  
        }}  
      />  
    </Panel>  
  );  
}  
  
/* ---------- MAIN VIEW ---------- */  
export default function OperatorInboxView() {  
  const theme = useTheme();  
  const [selectedConvoId, setSelectedConvoId] = useState<string | null>(null);  
  
  const conversationsMap = useChatStore((state) => state.conversations);  
  const messagesByConversation = useChatStore(  
    (state) => state.messagesByConversation  
  );  
  const loadingState = useChatStore((state) => state.loadingState);  
  
  const unreadCounts = useAnalyticsStore((state) => state.unreadCounts);  
  const priorityScores = useAnalyticsStore((state) => state.priorityScores);  
  
  const sortedConversations = useMemo(() => {  
    return Object.values(conversationsMap).sort((a, b) => {  
      const scoreA = priorityScores[a.conversationId] || 0;  
      const scoreB = priorityScores[b.conversationId] || 0;  
      return scoreB - scoreA;  
    });  
  }, [conversationsMap, priorityScores]);  
  
  const selectedMessages =  
    messagesByConversation[selectedConvoId || ''] || [];  
  const isLoading = loadingState === 'loading';  
  
  const handleSendMessage = (text: string) => {  
    console.log('Send message:', text);  
    // TODO: integrate with websocket send  
  };  
  
  return (  
    <Grid  
      container  
      spacing={2}  
      sx={{  
        height: 'calc(100vh - 64px - 48px)',  
        bgcolor: theme.vars.palette.background.default,  
        overflow: 'hidden',  
        p: 2,  
      }}  
    >  
      {/* Conversations List */}  
      <Grid size={{ xs: 12, md: 3 }} sx={{ minWidth: 280 }}>  
        <ChatListPane  
          conversations={sortedConversations}  
          selectedConvoId={selectedConvoId}  
          onSelectConvo={setSelectedConvoId}  
          unreadCounts={unreadCounts}  
          messagesByConversation={messagesByConversation}  
          isLoading={isLoading}  
        />  
      </Grid>  
  
      {/* Message Stream */}  
      <Grid size={{ xs: 12, md: 6 }} sx={{ flexGrow: 1, minWidth: 0 }}>  
        <MessageStreamPane  
          messages={selectedMessages}  
          isLoading={isLoading}  
          onSend={handleSendMessage}  
        />  
      </Grid>  
  
      {/* Fan360 Insights */}  
      <Grid size={{ xs: 12, md: 3 }} sx={{ minWidth: 280 }}>  
        <Fan360InsightsPane convoId={selectedConvoId} isLoading={isLoading} />  
      </Grid>  
    </Grid>  
  );  
}  