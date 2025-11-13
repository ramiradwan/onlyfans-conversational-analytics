import React from "react";  
import {  
  Box,  
  Typography,  
  Avatar,  
  Divider,  
  Chip,  
  Stack,  
  List,  
  ListItem,  
  ListItemText,  
  Paper,  
  Tooltip,  
  TextField,  
} from "@mui/material";  
import { useTheme } from "@mui/material/styles";  
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";  
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";  
import ArrowForwardIcon from "@mui/icons-material/ArrowForward";  
import Placeholder from "./Placeholder";  
import type { ChatForUI } from "../store/useChatStore";  
import { useChatStore } from "../store/useChatStore";  
  
interface Fan360SidebarProps {  
  fan?: ChatForUI;  
}  
  
export default function Fan360Sidebar({ fan }: Fan360SidebarProps) {  
  const theme = useTheme();  
  
  // ✅ Pull data via stable getters  
  const analytics = useChatStore((s) => s.getAnalytics());  
  const enrichmentResults = useChatStore((s) => s.getEnrichmentResults());  
  const activeChatId = useChatStore((s) => s.getActiveChatId());  
  const chatsForUI = useChatStore((s) => s.getChatsForUI());  
  
  // ✅ Resolve fan from prop or active chat  
  const resolvedFan =  
    fan ?? chatsForUI.find((c) => c.conversationId === activeChatId) ?? null;  
  
  const enrichment = resolvedFan  
    ? enrichmentResults[resolvedFan.conversationId] || null  
    : null;  
  
  const fmtNum = React.useCallback(  
    (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 0 }),  
    []  
  );  
  
  if (!resolvedFan) {  
    return <Placeholder title="No fan selected" />;  
  }  
  
  const SentimentBlock = () => {  
    if (typeof resolvedFan.sentimentScore !== "number") {  
      return <Placeholder title="No sentiment data" />;  
    }  
    const score = resolvedFan.sentimentScore;  
    let label = "Neutral";  
    let color = theme.palette.warning.main;  
    if (score > 0.6) {  
      label = "Positive";  
      color = theme.palette.success.main;  
    } else if (score < 0.4) {  
      label = "Negative";  
      color = theme.palette.error.main;  
    }  
    return (  
      <Paper variant="outlined" sx={{ p: 1, display: "inline-flex", alignItems: "center" }}>  
        <Tooltip title={`Sentiment: ${label} (${(score * 100).toFixed(0)}%)`}>  
          <Chip  
            label={`${label} ${(score * 100).toFixed(0)}%`}  
            sx={{  
              bgcolor: color,  
              color: theme.palette.getContrastText(color),  
              fontWeight: 600,  
            }}  
            aria-label={`Sentiment ${label} ${(score * 100).toFixed(0)} percent`}  
          />  
        </Tooltip>  
      </Paper>  
    );  
  };  
  
  const TopicsBlock = () => {  
    if (!analytics?.topics?.length) {  
      return <Placeholder title="No topics yet" />;  
    }  
    return (  
      <Paper  
        variant="outlined"  
        sx={{  
          maxHeight: 200,  
          overflowY: "auto",  
          bgcolor: theme.palette.action.hover,  
        }}  
      >  
        <List dense disablePadding>  
          {analytics.topics.map((t, idx) => {  
            let trendIcon: React.ReactNode = <ArrowForwardIcon fontSize="small" />;  
            let trendColor = theme.palette.info.main;  
            if (typeof t.trend === "number") {  
              if (t.trend > 0) {  
                trendIcon = <ArrowUpwardIcon fontSize="small" />;  
                trendColor = theme.palette.success.main;  
              } else if (t.trend < 0) {  
                trendIcon = <ArrowDownwardIcon fontSize="small" />;  
                trendColor = theme.palette.error.main;  
              }  
            }  
            return (  
              <ListItem key={idx} disableGutters>  
                <ListItemText  
                  primary={  
                    <Stack direction="row" spacing={0.5} alignItems="center">  
                      <Typography variant="body2">{t.topic}</Typography>  
                      <Box sx={{ color: trendColor }} aria-label={`Trend ${t.trend ?? "N/A"}`}>  
                        {trendIcon}  
                      </Box>  
                    </Stack>  
                  }  
                  secondary={`Volume: ${fmtNum(t.volume)} | Trend: ${t.trend ?? "N/A"}`}  
                  primaryTypographyProps={{ variant: "body2" }}  
                  secondaryTypographyProps={{ variant: "caption" }}  
                />  
              </ListItem>  
            );  
          })}  
        </List>  
      </Paper>  
    );  
  };  
  
  const SummaryBlock = () => {  
    if (!enrichment?.topics?.length) {  
      return <Placeholder title="No summary available" />;  
    }  
    const topicList = enrichment.topics.map((t) => t.description).join(", ");  
    return (  
      <Typography  
        variant="body2"  
        sx={{  
          p: 1,  
          bgcolor: theme.palette.action.hover,  
          borderRadius: theme.shape.borderRadius,  
        }}  
      >  
        This fan frequently engages around {topicList}.  
      </Typography>  
    );  
  };  
  
  return (  
    <Box sx={{ p: 2 }}>  
      {/* Profile Header */}  
      <Stack direction="row" spacing={2} alignItems="center">  
        <Avatar src={resolvedFan.avatarUrl} alt={resolvedFan.displayName || "Fan"} sx={{ width: 56, height: 56 }}>  
          {resolvedFan.displayName?.[0] ?? "?"}  
        </Avatar>  
        <Box>  
          <Typography variant="h6">{resolvedFan.displayName || "Unknown"}</Typography>  
          {resolvedFan.withUser?.username && (  
            <Typography variant="body2" color="text.secondary">  
              @{resolvedFan.withUser.username}  
            </Typography>  
          )}  
        </Box>  
      </Stack>  
  
      {/* Stats */}  
      <Box sx={{ mt: 2 }}>  
        <Typography variant="subtitle2" gutterBottom>  
          Conversation Stats  
        </Typography>  
        <Stack spacing={1}>  
          <Chip label={`Messages: ${fmtNum(resolvedFan.messageCount)}`} size="small" />  
          {resolvedFan.averageResponseTime !== undefined && (  
            <Chip label={`Avg Response: ${resolvedFan.averageResponseTime} min`} size="small" />  
          )}  
          {typeof resolvedFan.turns === "number" && (  
            <Chip label={`Turns: ${fmtNum(resolvedFan.turns)}`} size="small" />  
          )}  
          {resolvedFan.silencePercentage !== undefined && (  
            <Chip label={`Silence: ${resolvedFan.silencePercentage}%`} size="small" />  
          )}  
          {typeof resolvedFan.priorityScore === "number" && (  
            <Chip  
              label={`Priority: ${resolvedFan.priorityScore}`}  
              size="small"  
              sx={{  
                bgcolor:  
                  resolvedFan.priorityScore > 80  
                    ? theme.palette.success.light  
                    : theme.palette.info.light,  
                color: theme.palette.getContrastText(  
                  resolvedFan.priorityScore > 80  
                    ? theme.palette.success.light  
                    : theme.palette.info.light  
                ),  
              }}  
            />  
          )}  
        </Stack>  
      </Box>  
  
      <Divider sx={{ my: 2 }} />  
  
      {/* Sentiment */}  
      <Box sx={{ mb: 2 }}>  
        <Typography variant="subtitle2" gutterBottom>  
          Sentiment  
        </Typography>  
        <SentimentBlock />  
      </Box>  
  
      {/* Topics */}  
      <Typography variant="subtitle2" gutterBottom>  
        Topics  
      </Typography>  
      <TopicsBlock />  
  
      <Divider sx={{ my: 2 }} />  
  
      {/* AI Summary */}  
      <Typography variant="subtitle2" gutterBottom>  
        AI Summary  
      </Typography>  
      <SummaryBlock />  
  
      {/* Notes */}  
      <Box sx={{ mt: 2 }}>  
        <Typography  
          variant="subtitle2"  
          gutterBottom  
          component="label"  
          htmlFor="fan-notes"  
        >  
          Operator Notes  
        </Typography>  
        <TextField  
          id="fan-notes"  
          placeholder="Add notes here..."  
          multiline  
          minRows={3}  
          fullWidth  
        />  
      </Box>  
    </Box>  
  );  
}  