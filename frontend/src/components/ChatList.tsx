import React, { useMemo, useCallback } from "react";  
import {  
  Box,  
  Typography,  
  Chip,  
  useTheme,  
  Stack,  
  Paper,  
  Avatar,  
  Tooltip,  
} from "@mui/material";  
import Placeholder from "./Placeholder";  
import type { ChatForUI } from "../store/useChatStore";  
import { useChatStore } from "../store/useChatStore";  
  
interface ChatListProps {  
  chats?: ChatForUI[];  
  activeChatId?: string | null;  
  onSelect?: (id: string) => void;  
  sortByPriority?: boolean;  
  limit?: number;  
}  
  
const StatChip: React.FC<{  
  label: string;  
  title: string;  
  color: string;  
}> = React.memo(({ label, title, color }) => {  
  const theme = useTheme();  
  return (  
    <Tooltip title={title}>  
      <Chip  
        label={label}  
        size="small"  
        sx={{  
          bgcolor: color,  
          color: theme.palette.getContrastText(color),  
          fontWeight: 600,  
          borderRadius: 1,  
          height: 22,  
        }}  
      />  
    </Tooltip>  
  );  
});  
StatChip.displayName = "StatChip";  
  
const ChatList: React.FC<ChatListProps> = React.memo(  
  ({  
    chats: propChats,  
    activeChatId: propActiveChatId,  
    onSelect: propOnSelect,  
    sortByPriority = true,  
    limit,  
  }) => {  
    const theme = useTheme();  
  
    // ✅ Use getters for arrays and activeChatId  
    const storeChats = useChatStore((s) => s.getSortedChatsForUI());  
    const storeActiveChatId = useChatStore((s) => s.getActiveChatId());  
    const storeSetActiveChatId = useChatStore((s) => s.setActiveChatId);  
  
    const chats = propChats ?? storeChats;  
    const activeChatId = propActiveChatId ?? storeActiveChatId;  
    const onSelect = propOnSelect ?? storeSetActiveChatId;  
  
    if (!chats.length) {  
      return (  
        <Placeholder  
          title="No conversations yet"  
          subtitle="Waiting for snapshot data..."  
        />  
      );  
    }  
  
    const visibleChats = useMemo(() => {  
      const sorted = sortByPriority  
        ? [...chats].sort(  
            (a, b) => (b.priorityScore ?? 0) - (a.priorityScore ?? 0)  
          )  
        : chats;  
      return limit ? sorted.slice(0, limit) : sorted;  
    }, [chats, sortByPriority, limit]);  
  
    const handleKeyDown = useCallback(  
      (e: React.KeyboardEvent, chatId: string) => {  
        if (e.key === "Enter" || e.key === " ") {  
          e.preventDefault();  
          onSelect(chatId);  
        }  
      },  
      [onSelect]  
    );  
  
    return (  
      <Stack spacing={1.25}>  
        {visibleChats.map((chat) => {  
          const chatId = String(chat.conversationId);  
          const isActive = activeChatId === chatId;  
  
          const chips: React.ReactNode[] = [];  
  
          if (typeof chat.sentimentScore === "number") {  
            const pct = (chat.sentimentScore * 100).toFixed(0);  
            const color =  
              chat.sentimentScore > 0.6  
                ? theme.palette.success.main  
                : chat.sentimentScore < 0.4  
                ? theme.palette.error.main  
                : theme.palette.warning.main;  
            chips.push(  
              <StatChip  
                key={`${chat.conversationId}-chip-sentiment`}  
                label={`${pct}%`}  
                title={`Sentiment: ${pct}%`}  
                color={color}  
              />  
            );  
          }  
  
          if (typeof chat.priorityScore === "number") {  
            const color =  
              chat.priorityScore > 80  
                ? theme.palette.success.main  
                : theme.palette.info.light;  
            chips.push(  
              <StatChip  
                key={`${chat.conversationId}-chip-priority`}  
                label={`P${chat.priorityScore}`}  
                title={`Priority Score: ${chat.priorityScore}`}  
                color={color}  
              />  
            );  
          }  
  
          if (chat.unreadCount > 0) {  
            chips.push(  
              <StatChip  
                key={`${chat.conversationId}-chip-unread`}  
                label={String(chat.unreadCount)}  
                title={`${chat.unreadCount} unread message${  
                  chat.unreadCount > 1 ? "s" : ""  
                }`}  
                color={  
                  theme.palette.mode === "dark"  
                    ? theme.palette.primary.light  
                    : theme.palette.primary.main  
                }  
              />  
            );  
          }  
  
          return (  
            <Paper  
              key={chatId}  
              role="button"  
              aria-label={`Conversation with ${  
                chat.displayName ?? chatId  
              }${chat.unreadCount ? `, ${chat.unreadCount} unread` : ""}`}  
              tabIndex={0}  
              onClick={() => onSelect(chatId)}  
              onKeyDown={(e) => handleKeyDown(e, chatId)}  
              elevation={isActive ? 3 : 1}  
              sx={{  
                p: theme.spacing(1.5),  
                display: "flex",  
                alignItems: "center",  
                gap: 1.5,  
                cursor: "pointer",  
                borderRadius: theme.shape.borderRadius,  
                bgcolor: isActive  
                  ? theme.palette.action.selected  
                  : theme.palette.background.paper,  
                transition:  
                  "background-color 0.25s ease, box-shadow 0.25s ease, transform 0.15s ease",  
                "&:hover": {  
                  bgcolor: theme.palette.action.hover,  
                  boxShadow: theme.shadows[3],  
                  transform: "translateY(-1px)",  
                },  
                "&:focus-visible": {  
                  outline: `2px solid ${theme.palette.primary.main}`,  
                  outlineOffset: 2,  
                },  
              }}  
            >  
              <Avatar  
                src={chat.avatarUrl || undefined}  
                alt={chat.displayName ?? chatId}  
                sx={{  
                  width: 42,  
                  height: 42,  
                  fontWeight: 600,  
                  bgcolor: theme.palette.primary.light,  
                  color: theme.palette.primary.contrastText,  
                }}  
              >  
                {chat.avatarUrl  
                  ? null  
                  : (chat.displayName || chatId).charAt(0).toUpperCase()}  
              </Avatar>  
  
              <Box sx={{ flexGrow: 1, minWidth: 0 }}>  
                <Typography  
                  variant="subtitle1"  
                  noWrap  
                  fontWeight={600}  
                  color="text.primary"  
                >  
                  {chat.displayName ?? chatId}  
                </Typography>  
                <Typography  
                  variant="body2"  
                  color="text.secondary"  
                  noWrap  
                  sx={{ fontSize: "0.85rem" }}  
                >  
                  Messages: {chat.messageCount}  
                </Typography>  
              </Box>  
  
              <Stack  
                direction="row"  
                spacing={0.5}  
                flexShrink={0}  
                alignItems="center"  
              >  
                {chips}  
              </Stack>  
            </Paper>  
          );  
        })}  
      </Stack>  
    );  
  }  
);  
  
ChatList.displayName = "ChatList";  
export default ChatList;  