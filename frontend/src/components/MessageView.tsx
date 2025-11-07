import React from "react";  
import { Box, Typography, useTheme } from "@mui/material";  
import type { Message } from "../types/backend";  
  
interface MessageViewProps {  
  messages: Message[];  
  showDebug: boolean;  
}  
  
/**  
 * MessageView  
 * -----------  
 * Renders a conversation as message bubbles.  
 * Backend must provide:  
 *  - `is_creator` boolean  
 *  - `createdAt` ISO timestamp  
 *  - `replyToMessage.text` if replying  
 *  - `text` cleaned, media info if needed  
 */  
export default function MessageView({ messages, showDebug }: MessageViewProps) {  
  const theme = useTheme();  
  const messagesEndRef = React.useRef<HTMLDivElement>(null);  
  const containerRef = React.useRef<HTMLDivElement>(null);  
  
  React.useEffect(() => {  
    if (containerRef.current) {  
      const isMobile = window.innerWidth < 900;  
      if (isMobile) {  
        const scrollHeight = containerRef.current.scrollHeight;  
        const offset = containerRef.current.offsetHeight * 0.3;  
        containerRef.current.scrollTop = scrollHeight - offset;  
      } else if (messagesEndRef.current) {  
        messagesEndRef.current.scrollIntoView({ behavior: "auto" });  
      }  
    }  
  }, [messages]);  
  
  if (!messages?.length) return null;  
  
  let lastSender: boolean | null = null;  
  
  return (  
    <Box  
      ref={containerRef}  
      sx={{  
        display: "flex",  
        flexDirection: "column",  
        alignItems: "center",  
        pb: { xs: showDebug ? "30vh" : 2, md: 2 },  
      }}  
    >  
      {messages.map((msg) => {  
        const isSameSender = lastSender === msg.is_creator;  
        lastSender = msg.is_creator;  
  
        return (  
          <Box  
            key={String(msg.id)}  
            sx={{  
              alignSelf: msg.is_creator ? "flex-end" : "flex-start",  
              bgcolor: msg.is_creator  
                ? theme.palette.primary.light  
                : theme.palette.grey[200],  
              color: msg.is_creator  
                ? theme.palette.primary.contrastText  
                : theme.palette.text.primary,  
              borderTopRightRadius: msg.is_creator ? 0 : 12,  
              borderTopLeftRadius: msg.is_creator ? 12 : 0,  
              borderBottomLeftRadius: 12,  
              borderBottomRightRadius: 12,  
              p: 1.5,  
              mb: isSameSender ? 0.5 : 1.5,  
              maxWidth: "600px",  
              width: "fit-content",  
              boxShadow: theme.shadows[1],  
            }}  
          >  
            {!isSameSender && (  
              <Box  
                sx={{  
                  fontSize: "0.75rem",  
                  color: msg.is_creator  
                    ? "rgba(255,255,255,0.8)"  
                    : theme.palette.text.secondary,  
                  mb: 0.5,  
                  display: "flex",  
                  justifyContent: "space-between",  
                }}  
              >  
                <span>{msg.is_creator ? "Creator" : "Fan"}</span>  
                <span>  
                  {msg.createdAt  
                    ? new Date(msg.createdAt).toLocaleTimeString([], {  
                        hour: "2-digit",  
                        minute: "2-digit",  
                      })  
                    : ""}  
                </span>  
              </Box>  
            )}  
            {msg.replyToMessage && (  
              <Box  
                sx={{  
                  bgcolor: msg.is_creator  
                    ? "rgba(255,255,255,0.15)"  
                    : theme.palette.grey[300],  
                  borderLeft: `3px solid ${  
                    msg.is_creator  
                      ? theme.palette.primary.contrastText  
                      : theme.palette.grey[500]  
                  }`,  
                  color: msg.is_creator  
                    ? theme.palette.primary.contrastText  
                    : theme.palette.text.secondary,  
                  fontSize: "0.85rem",  
                  p: "6px 10px",  
                  mb: "6px",  
                  borderRadius: "6px",  
                }}  
              >  
                <strong>Replying to:</strong>{" "}  
                {msg.replyToMessage.text?.slice(0, 120) || "(media)"}  
              </Box>  
            )}  
            <Typography  
              variant="body1"  
              sx={{ whiteSpace: "pre-wrap", fontSize: "0.95rem" }}  
            >  
              {msg.text || (msg.media?.length ? "(media)" : "")}  
            </Typography>  
          </Box>  
        );  
      })}  
      <div ref={messagesEndRef} />  
    </Box>  
  );  
}  