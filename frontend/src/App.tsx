import React from "react";  
import {  
  ThemeProvider,  
  createTheme,  
  CssBaseline,  
  AppBar,  
  Toolbar,  
  IconButton,  
  Typography,  
  Box,  
  Drawer,  
} from "@mui/material";  
  
import ChatList from "./components/ChatList";  
import MessageView from "./components/MessageView";  
import DebugPanel from "./components/DebugPanel";  
import Placeholder from "./components/Placeholder";  
import { AppConfig } from "./types";  
  
// ✅ Import backend‑generated types  
import { components } from "./types/backend";  
type Chat = components["schemas"]["ChatThread"];  
type ChatMessage = components["schemas"]["Message"];  
  
const lightTheme = createTheme({ palette: { mode: "light" } });  
  
interface AppProps {  
  config: AppConfig;  
}  
    
export default function App({ config }: AppProps) {  
  const [logs, setLogs] = React.useState<string[]>([]);  
  const [chats, setChats] = React.useState<Chat[]>([]);  
  const [messages, setMessages] = React.useState<ChatMessage[]>([]);  
  const [activeChatId, setActiveChatId] = React.useState<string | null>(null);  
  const [showDebug, setShowDebug] = React.useState(true);  
  const [mobileChatsOpen, setMobileChatsOpen] = React.useState(false);  
  
  const wsRef = React.useRef<WebSocket | null>(null);  
  
  const addLog = React.useCallback((message: string) => {  
    setLogs((prev) => {  
      const updated = [  
        ...prev,  
        `[${new Date().toLocaleTimeString()}] ${message}`,  
      ];  
      return updated.slice(-50);  
    });  
  }, []);  
  
  // --- Send commands from backend → extension ---  
  const sendMessageToExtension = React.useCallback(  
    (type: string, payload: any = {}): Promise<any> => {  
      return new Promise((resolve) => {  
        try {  
          if (!chrome?.runtime) {  
            addLog("Chrome runtime not available.");  
            return resolve(null);  
          }  
          chrome.runtime.sendMessage(  
            config.EXTENSION_ID,  
            { type, ...payload },  
            (response) => {  
              if (chrome.runtime.lastError) {  
                addLog(  
                  `Extension error: ${chrome.runtime.lastError.message}`  
                );  
                resolve(null);  
              } else {  
                resolve(response);  
              }  
            }  
          );  
        } catch (err: any) {  
          addLog(`Failed to send message to extension: ${err.message}`);  
          resolve(null);  
        }  
      });  
    },  
    [config.EXTENSION_ID, addLog]  
  );  
  
  // --- Connect to FastAPI WS and listen for pushes ---  
  const connectFastAPI = React.useCallback(() => {  
    wsRef.current = new WebSocket(config.FASTAPI_WS_URL);  
  
    wsRef.current.onopen = () => {  
      addLog("Connected to FastAPI WS (frontend).");  
    };  
  
    wsRef.current.onmessage = (event) => {  
      const msg = JSON.parse(event.data);  
      addLog(`WS: ${event.data.slice(0, 150)}...`);  
  
      switch (msg.type) {  
        case "cache_update":  
          // Backend now sends chats with .messages attached  
          setChats(msg.chats || []);  
          setMessages((prev) => {  
            // If we have an active chat, update its messages immediately  
            if (activeChatId) {  
              const activeChat = (msg.chats || []).find(  
                (c: Chat) => String(c.id) === String(activeChatId)  
              );  
              if (activeChat?.messages?.length) {  
                return activeChat.messages;  
              }  
            }  
            return prev;  
          });  
          addLog(  
            `Received cache_update: ${msg.chats?.length || 0} chats, ${  
              msg.messages?.length || 0  
            } messages.`  
          );  
          break;  
  
        case "messages_for_chat":  
          if (msg.payload.chat_id === activeChatId) {  
            setMessages(msg.payload.messages);  
            addLog(  
              `Received ${msg.payload.messages.length} messages for chat ${activeChatId}.`  
            );  
          }  
          break;  
  
        case "send_message_command":  
          sendMessageToExtension("send_message_command", msg.payload);  
          break;  
  
        case "mark_as_read_command":  
          sendMessageToExtension("mark_as_read_command", msg.payload);  
          break;  
      }  
    };  
  
    wsRef.current.onclose = () => {  
      addLog("WS closed. Reconnecting...");  
      setTimeout(connectFastAPI, 3000);  
    };  
  
    wsRef.current.onerror = (err) => {  
      addLog(`WS error: ${err}`);  
      wsRef.current?.close();  
    };  
  }, [config.FASTAPI_WS_URL, addLog, sendMessageToExtension, activeChatId]);  
  
  React.useEffect(() => {  
    addLog("App mounted.");  
    connectFastAPI();  
  }, [connectFastAPI]);  
  
  // --- Updated handleSelectChat ---  
  const handleSelectChat = (chatId: string) => {  
    setActiveChatId(chatId);  
  
    // Try to find messages from local chats state first  
    const selectedChat = chats.find((c) => String(c.id) === String(chatId));  
    if (selectedChat?.messages?.length) {  
      setMessages(selectedChat.messages);  
      addLog(  
        `Loaded ${selectedChat.messages.length} messages for chat ${chatId} from local cache.`  
      );  
    } else {  
      setMessages([]);  
      if (wsRef.current?.readyState === WebSocket.OPEN) {  
        addLog(`Requesting messages for chat ${chatId} from backend...`);  
        wsRef.current.send(  
          JSON.stringify({  
            type: "get_messages_for_chat",  
            payload: { chat_id: chatId },  
          })  
        );  
      }  
    }  
  
    setMobileChatsOpen(false);  
  };  
  
  // --- JSX below remains the same ---  
  return (  
    <ThemeProvider theme={lightTheme}>  
      <CssBaseline />  
      <AppBar position="static" color="primary">  
        <Toolbar>  
          <IconButton  
            color="inherit"  
            sx={{ display: { xs: "inline-flex", md: "none" }, mr: 1 }}  
            onClick={() => setMobileChatsOpen(true)}  
          >  
            <span className="material-icons">menu</span>  
          </IconButton>  
          <Typography variant="h6" sx={{ flexGrow: 1 }}>  
            Conversational Analytics  
          </Typography>  
          <IconButton  
            color="inherit"  
            onClick={() => setShowDebug(!showDebug)}  
          >  
            <span className="material-icons">bug_report</span>  
          </IconButton>  
        </Toolbar>  
      </AppBar>  
  
      {/* Mobile Drawer */}  
      <Drawer  
        anchor="left"  
        open={mobileChatsOpen}  
        onClose={() => setMobileChatsOpen(false)}  
        sx={{ display: { xs: "block", md: "none" } }}  
      >  
        <Box sx={{ width: 300, p: 1, overflowY: "auto" }}>  
          {chats.length > 0 ? (  
            <ChatList  
              chats={chats}  
              activeChatId={activeChatId}  
              onSelect={handleSelectChat}  
            />  
          ) : (  
            <Placeholder title="No chats yet" subtitle="Waiting for data..." />  
          )}  
        </Box>  
      </Drawer>  
  
      {/* Mobile Content */}  
      <Box  
        sx={{  
          display: { xs: "block", md: "none" },  
          height: "calc(100vh - 64px)",  
          overflowY: "auto",  
        }}  
      >  
        {activeChatId ? (  
          messages.length > 0 ? (  
            <MessageView messages={messages} showDebug={showDebug} />  
          ) : (  
            <Placeholder title="Loading messages..." />  
          )  
        ) : (  
          <Box sx={{ p: 1 }}>  
            <ChatList  
              chats={chats}  
              activeChatId={activeChatId}  
              onSelect={handleSelectChat}  
            />  
          </Box>  
        )}  
      </Box>  
  
      {/* Desktop Layout */}  
      <Box  
        sx={{  
          display: { xs: "none", md: "flex" },  
          height: "calc(100vh - 64px)",  
          gap: 2,  
        }}  
      >  
        <Box  
          sx={{  
            width: 300,  
            borderRight: 1,  
            borderColor: "divider",  
            overflowY: "auto",  
          }}  
        >  
          {chats.length > 0 ? (  
            <ChatList  
              chats={chats}  
              activeChatId={activeChatId}  
              onSelect={handleSelectChat}  
            />  
          ) : (  
            <Placeholder title="No chats yet" subtitle="Waiting for data..." />  
          )}  
        </Box>  
        <Box  
          sx={{  
            flex: 1,  
            overflowY: "auto",  
            display: "flex",  
            justifyContent: "center",  
          }}  
        >  
          <Box sx={{ width: "100%", maxWidth: 800, px: 2 }}>  
            {activeChatId ? (  
              messages.length > 0 ? (  
                <MessageView messages={messages} showDebug={showDebug} />  
              ) : (  
                <Placeholder title="Loading messages..." />  
              )  
            ) : (  
              <Placeholder  
                title="Select a chat"  
                subtitle="Choose from the list on the left"  
              />  
            )}  
          </Box>  
        </Box>  
        <Box  
          sx={{  
            width: 300,  
            borderLeft: 1,  
            borderColor: "divider",  
            overflowY: "auto",  
          }}  
        >  
          <DebugPanel logs={logs} visible={showDebug} />  
        </Box>  
      </Box>  
  
      {/* Mobile Debug Panel */}  
      <DebugPanel logs={logs} visible={showDebug} isMobile={true} />  
    </ThemeProvider>  
  );  
}  