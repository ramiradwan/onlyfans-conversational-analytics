import React from "react";  
import {  
  AppBar,  
  Toolbar,  
  IconButton,  
  Typography,  
  Box,  
  Drawer,  
  Container,  
  Paper,  
} from "@mui/material";  
import MenuIcon from "@mui/icons-material/Menu";  
import BugReportIcon from "@mui/icons-material/BugReport";  
import SearchIcon from "@mui/icons-material/Search";  
import FilterListIcon from "@mui/icons-material/FilterList";  
import ConnectionStatusIndicator from "./components/ConnectionStatusIndicator";  
import CalmTriageView from "./views/CalmTriageView";  
import PerformanceView from "./views/PerformanceView";  
import InboxView from "./views/InboxView";  
import ChatList from "./components/ChatList";  
import DebugPanel from "./components/DebugPanel";  
import { theme } from "./theme";  
import { useChatStore } from "./store/useChatStore";  
import type { Permissions } from "./hooks/usePermissions";  
  
export interface AppLayoutProps {  
  permissions: Permissions;  
  showDebug: boolean;  
  toggleDebug: () => void;  
  mobileChatsOpen: boolean;  
  openMobileChats: () => void;  
  closeMobileChats: () => void;  
}  
  
export default function AppLayout({  
  permissions,  
  showDebug,  
  toggleDebug,  
  mobileChatsOpen,  
  openMobileChats,  
  closeMobileChats,  
}: AppLayoutProps) {  
  const isMobile = /Mobi|Android/i.test(navigator.userAgent);  
  const sortedChats = useChatStore((s) => s.getSortedChatsForUI());  
  const activeChatId = useChatStore((s) => s.getActiveChatId());  
  const setActiveChatId = useChatStore((s) => s.setActiveChatId);  
  
  return (  
    <>  
      <AppBar  
        position="sticky"  
        enableColorOnDark  
        sx={{  
          background: "linear-gradient(90deg, #2563EB 0%, #1E40AF 100%)",  
        }}  
      >  
        <Toolbar variant="dense" sx={{ minHeight: 56 }}>  
          <IconButton  
            color="inherit"  
            sx={{ display: { xs: "inline-flex", md: "none" }, mr: 1 }}  
            onClick={openMobileChats}  
            size="large"  
          >  
            <MenuIcon />  
          </IconButton>  
          <Typography variant="h6" sx={{ flexGrow: 1, fontWeight: 600 }} noWrap>  
            Conversational Analytics  
          </Typography>  
          <IconButton color="inherit" size="large">  
            <SearchIcon />  
          </IconButton>  
          <IconButton color="inherit" size="large">  
            <FilterListIcon />  
          </IconButton>  
          <ConnectionStatusIndicator />  
          <IconButton color="inherit" onClick={toggleDebug} size="large">  
            <BugReportIcon />  
          </IconButton>  
        </Toolbar>  
      </AppBar>  
  
      <Box sx={{ display: "flex", height: "calc(100vh - 56px)", gap: 3 }}>  
        <Container maxWidth={false} sx={{ flex: 1, overflow: "auto", py: 2 }}>  
          {permissions.isCreator && <CalmTriageView />}  
          {permissions.isManager && <PerformanceView />}  
          {permissions.isOperator && <InboxView />}  
        </Container>  
  
        {!isMobile && showDebug && (  
          <Paper  
            sx={{  
              width: theme.spacing(50),  
              borderLeft: `1px solid ${theme.palette.divider}`,  
            }}  
          >  
            <DebugPanel />  
          </Paper>  
        )}  
      </Box>  
  
      <Drawer anchor="left" open={mobileChatsOpen} onClose={closeMobileChats}>  
        <Box sx={{ width: theme.spacing(37.5), p: 2 }}>  
          <Typography variant="h6">Chats</Typography>  
          <ChatList  
            chats={sortedChats}  
            activeChatId={activeChatId}  
            onSelect={(id) => setActiveChatId(id)}  
            sortByPriority={false}  
          />  
        </Box>  
      </Drawer>  
  
      {isMobile && (  
        <Drawer anchor="bottom" open={showDebug} onClose={toggleDebug}>  
          <DebugPanel />  
        </Drawer>  
      )}  
    </>  
  );  
}  