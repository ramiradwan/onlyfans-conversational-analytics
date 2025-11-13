import React from "react";  
import {  
  Paper,  
  Box,  
  Skeleton,  
  Button,  
  useTheme,  
  Typography,  
} from "@mui/material";  
import Grid from "@mui/material/Grid";  
import ChatList from "../components/ChatList";  
import MessageView from "../components/MessageView";  
import Fan360Sidebar from "../components/Fan360Sidebar";  
import Placeholder from "../components/Placeholder";  
import { useChatStore } from "../store/useChatStore";  
import { renderSkeletons } from "../utils/renderSkeletons";  
  
export default function InboxView() {  
  const theme = useTheme();  
  
  // ✅ Limit handled in store with cached slice  
  const chats = useChatStore((s) => s.getSortedChatsForUI(10));  
  
  // ✅ Getter for active chat ID  
  const activeChatId = useChatStore((s) => s.getActiveChatId());  
  const setActiveChatId = useChatStore((s) => s.setActiveChatId);  
  
  // ✅ Safe getter — returns stable reference  
  const messages = useChatStore((s) => s.getMessagesForChat(activeChatId));  
  
  // ✅ Getter for system status  
  const status = useChatStore((s) => s.getSystemStatus());  
  
  const hasChats = chats.length > 0;  
  const activeFan = chats.find((c) => c.conversationId === activeChatId);  
  
  const showSkeletons = status === "PROCESSING_SNAPSHOT";  
  
  return (  
    <Grid container spacing={2} sx={{ height: "100%", p: 2 }}>  
      {/* Chats list */}  
      <Grid size={{ xs: 12, md: 3 }} sx={{ overflowY: "auto" }}>  
        <Paper sx={{ p: 2, height: "100%" }}>  
          <Typography variant="h6" sx={{ mb: 2 }}>  
            Chats  
          </Typography>  
          {showSkeletons ? (  
            renderSkeletons(5, 64, 1)  
          ) : hasChats ? (  
            <ChatList  
              chats={chats}  
              activeChatId={activeChatId}  
              onSelect={setActiveChatId}  
              sortByPriority={false}  
            />  
          ) : (  
            <Placeholder title="No chats yet" subtitle="Waiting for data..." />  
          )}  
        </Paper>  
      </Grid>  
  
      {/* Messages view */}  
      <Grid size={{ xs: 12, md: 6 }} sx={{ overflowY: "auto" }}>  
        <Paper sx={{ p: 2, height: "100%" }}>  
          <Typography variant="h6" sx={{ mb: 2 }}>  
            Messages  
          </Typography>  
          {showSkeletons ? (  
            renderSkeletons(6, 32, 1)  
          ) : activeChatId ? (  
            messages.length > 0 ? (  
              <MessageView messages={messages} />  
            ) : (  
              <Placeholder title="No messages" subtitle="Start the conversation" />  
            )  
          ) : (  
            <Placeholder title="Select a chat" subtitle="Choose from the list" />  
          )}  
        </Paper>  
      </Grid>  
  
      {/* Fan Insights */}  
      <Grid size={{ xs: 12, md: 3 }} sx={{ overflowY: "auto" }}>  
        <Paper sx={{ p: 2, height: "100%" }}>  
          <Typography variant="h6" sx={{ mb: 2 }}>  
            Fan Insights  
          </Typography>  
          {showSkeletons ? (  
            <Skeleton height={48} sx={{ mb: 2 }} />  
          ) : activeChatId && activeFan ? (  
            <Fan360Sidebar fan={activeFan} />  
          ) : (  
            <Placeholder title="No fan selected" />  
          )}  
        </Paper>  
      </Grid>  
    </Grid>  
  );  
}  