import React from "react";  
import { Box, Typography, Chip, Paper, Stack, Tooltip } from "@mui/material";  
import { useTheme } from "@mui/material/styles";  
import Placeholder from "./Placeholder";  
import type { Message } from "../types/backend-wss";  
  
interface MessageViewProps {  
  messages: Message[];  
  showDebug?: boolean; // optional  
  activeMessageId?: string | null; // NEW for highlighting  
}  
  
export default function MessageView({  
  messages,  
  showDebug = false,  
  activeMessageId = null,  
}: MessageViewProps) {  
  const theme = useTheme();  
  const br = theme.shape.borderRadius as number;  
  const messagesEndRef = React.useRef<HTMLDivElement>(null);  
  
  // Auto-scroll to bottom when messages change  
  React.useEffect(() => {  
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });  
  }, [messages]);  
  
  if (!messages?.length) {  
    return <Placeholder title="No messages" subtitle="Start the conversation" />;  
  }  
  
  const formatTimestamp = React.useCallback((ts?: string | null) => {  
    if (!ts) return "";  
    const date = new Date(ts);  
    const diffMs = Date.now() - date.getTime();  
    const diffMin = Math.floor(diffMs / 60000);  
    if (diffMin < 60) return `${diffMin}m ago`;  
    if (diffMin < 1440) return `${Math.floor(diffMin / 60)}h ago`;  
    return date.toLocaleDateString();  
  }, []);  
  
  const SentimentBadge = ({ score }: { score?: number | null }) => {  
    if (typeof score !== "number") return null;  
    let color = theme.palette.warning.main;  
    let label = "Neutral";  
    if (score > 0.6) {  
      color = theme.palette.success.main;  
      label = "Positive";  
    } else if (score < 0.4) {  
      color = theme.palette.error.main;  
      label = "Negative";  
    }  
    const pct = (score * 100).toFixed(0);  
    return (  
      <Tooltip title={`Sentiment: ${label} (${pct}%)`}>  
        <Chip  
          label={`${label} ${pct}%`}  
          size="small"  
          sx={{  
            bgcolor: color,  
            color: theme.palette.getContrastText(color),  
            mb: 0.5,  
            fontWeight: 600,  
          }}  
        />  
      </Tooltip>  
    );  
  };  
  
  const isActiveBubble = (msg: Message) =>  
    activeMessageId != null && String(msg.id) === String(activeMessageId);  
  
  return (  
    <Box  
      role="list"  
      sx={{  
        display: "flex",  
        flexDirection: "column",  
        alignItems: "center",  
        pb: { xs: showDebug ? "30vh" : theme.spacing(2), md: theme.spacing(2) },  
        px: theme.spacing(1),  
        overflowY: "auto",  
        height: "100%",  
      }}  
    >  
      {messages.map((msg, idx) => {  
        const prevMsg = messages[idx - 1];  
        const isSameSender = prevMsg?.is_creator === msg.is_creator;  
        const bubbleBg = msg.is_creator  
          ? theme.palette.background.paper  
          : theme.palette.action.selected;  
  
        return (  
          <Paper  
            role="listitem"  
            aria-label={`Message from ${msg.is_creator ? "Creator" : "Fan"} at ${formatTimestamp(  
              msg.createdAt  
            )}`}  
            key={String(msg.id)}  
            elevation={isActiveBubble(msg) ? 4 : 1} // higher elevation if active  
            sx={{  
              alignSelf: msg.is_creator ? "flex-end" : "flex-start",  
              bgcolor: bubbleBg,  
              color: theme.palette.text.primary,  
              borderTopRightRadius: msg.is_creator ? 0 : br * 2,  
              borderTopLeftRadius: msg.is_creator ? br * 2 : 0,  
              borderBottomLeftRadius: br * 2,  
              borderBottomRightRadius: br * 2,  
              p: theme.spacing(1.5),  
              mb: isSameSender ? theme.spacing(0.5) : theme.spacing(1.5),  
              maxWidth: 520,  
              transition: "transform 0.15s ease, box-shadow 0.15s ease",  
              "&:hover": { transform: "translateY(-1px)" },  
              "&:focus-visible": {  
                outline: `2px solid ${theme.palette.primary.main}`,  
                outlineOffset: 2,  
              },  
              ...(isActiveBubble(msg)  
                ? {  
                    outline: `2px solid ${theme.palette.secondary.main}`,  
                    outlineOffset: 2,  
                  }  
                : {}),  
            }}  
            tabIndex={0}  
          >  
            {!isSameSender && (  
              <Box  
                sx={{  
                  fontSize: theme.typography.caption.fontSize,  
                  color: theme.palette.text.secondary,  
                  mb: theme.spacing(0.5),  
                  display: "flex",  
                  justifyContent: "space-between",  
                }}  
              >  
                <span>{msg.is_creator ? "Creator" : "Fan"}</span>  
                <Tooltip  
                  title={msg.createdAt ? new Date(msg.createdAt).toLocaleString() : ""}  
                >  
                  <span>{formatTimestamp(msg.createdAt)}</span>  
                </Tooltip>  
              </Box>  
            )}  
  
            <SentimentBadge score={msg.sentimentScore} />  
  
            {Array.isArray(msg.topics) && msg.topics.length > 0 && (  
              <Stack  
                direction="row"  
                spacing={0.5}  
                flexWrap="wrap"  
                mb={0.5}  
                aria-label="Message topics"  
              >  
                {msg.topics.map((topic, tIdx) => (  
                  <Chip  
                    key={`${topic}-${tIdx}`}  
                    label={topic}  
                    size="small"  
                    variant="outlined"  
                  />  
                ))}  
              </Stack>  
            )}  
  
            {msg.replyToMessage && (  
              <Box  
                sx={{  
                  bgcolor: theme.palette.action.hover,  
                  borderLeft: `3px solid ${theme.palette.divider}`,  
                  fontSize: theme.typography.body2.fontSize,  
                  p: `${theme.spacing(0.75)} ${theme.spacing(1.25)}`,  
                  mb: theme.spacing(0.75),  
                  borderRadius: br,  
                }}  
              >  
                <strong>  
                  Replying to {msg.replyToMessage.is_creator ? "Creator" : "Fan"}:  
                </strong>{" "}  
                {msg.replyToMessage.text?.slice(0, 120) || "(media)"}  
              </Box>  
            )}  
  
            <Typography variant="body1" sx={{ whiteSpace: "pre-wrap" }}>  
              {msg.text || (msg.media?.length ? "(media)" : "")}  
            </Typography>  
          </Paper>  
        );  
      })}  
      <div ref={messagesEndRef} />  
    </Box>  
  );  
}  