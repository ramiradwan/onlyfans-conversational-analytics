import React from "react";  
import { Box, Typography, useTheme } from "@mui/material";  
import type { ChatThread } from "../types/backend"; // Adjust if your schema name differs  
  
interface ChatListProps {  
  chats: ChatThread[];  
  activeChatId: string | null;  
  onSelect: (id: string) => void;  
}  
  
/**  
 * ChatList  
 * --------  
 * Renders a list of chats with user info and last message snippet.  
 * Assumes backend already normalizes:  
 *  - `withUser.name` / `withUser.username`  
 *  - `last_message.text` / `last_message.created_at`  
 */  
export default function ChatList({ chats, activeChatId, onSelect }: ChatListProps) {  
  const theme = useTheme();  
  
  if (!chats?.length) return null;  
  
  return (  
    <Box>  
      {chats.map((chat) => (  
        <Box  
          key={String(chat.id)}  
          onClick={() => onSelect(String(chat.id))}  
          sx={{  
            p: 1.5,  
            mb: 1,  
            borderRadius: 1,  
            cursor: "pointer",  
            bgcolor:  
              String(activeChatId) === String(chat.id)  
                ? theme.palette.action.selected  
                : theme.palette.background.paper,  
            "&:hover": { bgcolor: theme.palette.action.hover },  
          }}  
        >  
          <Typography variant="subtitle1" noWrap>  
            {chat.withUser?.name || "Unknown"}  
          </Typography>  
          {chat.withUser?.username && (  
            <Typography variant="body2" color="text.secondary">  
              @{chat.withUser.username}  
            </Typography>  
          )}  
          {chat.last_message?.text && (  
            <Typography variant="body2" noWrap color="text.secondary">  
              {chat.last_message.text}  
            </Typography>  
          )}  
        </Box>  
      ))}  
    </Box>  
  );  
}  