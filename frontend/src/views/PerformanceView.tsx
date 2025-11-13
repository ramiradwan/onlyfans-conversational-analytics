import React from "react";  
import { Box, Typography, Paper, Divider, useTheme } from "@mui/material";  
import ChatList from "../components/ChatList";  
import Placeholder from "../components/Placeholder";  
import KPIGroup from "../components/KPIGroup";  
import { useChatStore } from "../store/useChatStore";  
import { renderSkeletons } from "../utils/renderSkeletons";  
  
export default function PerformanceView() {  
  const theme = useTheme();  
  
  // ✅ Use public getter for stable sorted chats  
  const sortedChats = useChatStore((s) => s.getSortedChatsForUI());  
  
  // ✅ Use getter for system status  
  const status = useChatStore((s) => s.getSystemStatus());  
  
  // ✅ Use getter for active chat ID  
  const activeChatId = useChatStore((s) => s.getActiveChatId());  
  
  // Setter can be called directly  
  const setActiveChatId = useChatStore((s) => s.setActiveChatId);  
  
  const hasChats = sortedChats.length > 0;  
  const showSkeletons = status === "PROCESSING_SNAPSHOT";  
  
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
      {/* Team KPIs */}  
      <Paper  
        elevation={1}  
        sx={{ p: 2, borderRadius: 2, bgcolor: "background.paper" }}  
      >  
        <Typography  
          variant="h6"  
          sx={{ mb: 2, fontWeight: 600 }}  
          aria-label="Team Performance KPIs"  
        >  
          Team Performance KPIs  
        </Typography>  
        {showSkeletons ? renderSkeletons(3, 80) : <KPIGroup />}  
      </Paper>  
  
      <Divider />  
  
      {/* Team Inbox */}  
      <Paper  
        elevation={1}  
        sx={{ p: 2, flex: 1, overflowY: "auto", borderRadius: 2 }}  
      >  
        <Typography  
          variant="h6"  
          sx={{ mb: 2, fontWeight: 600 }}  
          aria-label="Team Inbox"  
        >  
          Team Inbox  
        </Typography>  
        {showSkeletons ? (  
          renderSkeletons(6, 64, 1)  
        ) : hasChats ? (  
          <ChatList  
            activeChatId={activeChatId}  
            onSelect={setActiveChatId}  
            sortByPriority={false}  
            limit={10}  
          />  
        ) : (  
          <Placeholder  
            title="No team chats"  
            subtitle="Waiting for activity..."  
          />  
        )}  
      </Paper>  
    </Box>  
  );  
}  