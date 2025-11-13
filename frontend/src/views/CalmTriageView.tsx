import React from "react";  
import { Box, Typography, Paper, Divider, useTheme } from "@mui/material";  
import ChatList from "../components/ChatList";  
import Placeholder from "../components/Placeholder";  
import KPIGroup from "../components/KPIGroup";  
import { useChatStore } from "../store/useChatStore";  
import { renderSkeletons } from "../utils/renderSkeletons";  
  
export default function CalmTriageView() {  
  const theme = useTheme();  
  
  // ✅ Use public getter for stable sorted chats  
  const sortedChats = useChatStore((s) => s.getSortedChatsForUI());  
  
  // ✅ Use getter for system status  
  const status = useChatStore((s) => s.getSystemStatus());  
  
  // ✅ Use getter for active chat ID  
  const activeChatId = useChatStore((s) => s.getActiveChatId());  
  
  // Setter remains direct — actions can be called directly  
  const setActiveChatId = useChatStore((s) => s.setActiveChatId);  
  
  const showSkeletons = status === "PROCESSING_SNAPSHOT";  
  const hasChats = sortedChats.length > 0;  
  
  return (  
    <Box  
      sx={{  
        p: 2,  
        height: "100%",  
        display: "flex",  
        flexDirection: "column",  
        gap: 3,  
        bgcolor: "background.default",  
      }}  
    >  
      {/* Engagement KPIs */}  
      <Paper  
        elevation={1}  
        sx={{ p: 2, borderRadius: 2, bgcolor: "background.paper" }}  
      >  
        <Typography  
          variant="h6"  
          sx={{ mb: 2, fontWeight: 600 }}  
          aria-label="Engagement KPIs"  
        >  
          Engagement KPIs  
        </Typography>  
        {showSkeletons ? renderSkeletons(3, 80) : <KPIGroup />}  
      </Paper>  
  
      <Divider />  
  
      {/* Priority Inbox */}  
      <Paper  
        elevation={1}  
        sx={{ p: 2, flex: 1, overflowY: "auto", borderRadius: 2 }}  
      >  
        <Typography  
          variant="h6"  
          sx={{ mb: 2, fontWeight: 600 }}  
          aria-label="Priority Inbox"  
        >  
          Priority Inbox  
        </Typography>  
        {showSkeletons ? (  
          renderSkeletons(5, 64)  
        ) : hasChats ? (  
          <ChatList  
            activeChatId={activeChatId}  
            onSelect={setActiveChatId}  
            sortByPriority  
            limit={8}  
          />  
        ) : (  
          <Placeholder  
            title="No priority chats"  
            subtitle="Waiting for data..."  
          />  
        )}  
      </Paper>  
    </Box>  
  );  
}  