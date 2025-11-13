import { useEffect, useRef } from "react";  
import useWebSocket from "react-use-websocket";  
import { useChatStore } from "../store/useChatStore";  
import { useDebugStore } from "../store/useDebugStore";  
  
/**  
 * Hook to manage Brain <-> Bridge WebSocket connection.  
 * Pure side-effect:  
 *  - Connects & reconnects with backoff.  
 *  - Forwards raw messages to store for parsing/dispatch.  
 *  - Updates connection status in store.  
 *  - Logs connection events to debug store.  
 */  
export function useSocket(wsUrl: string) {  
  const handleWssMessage = useChatStore((s) => s.handleWssMessage);  
  const setConnectionStatus = useChatStore((s) => s.setConnectionStatus);  
  const addLog = useDebugStore((s) => s.addLog);  
  
  const reconnectAttempts = useRef(0);  
  
  const { lastMessage, readyState } = useWebSocket(wsUrl, {  
    shouldReconnect: () => true,  
    reconnectAttempts: Infinity,  
    reconnectInterval: () => {  
      const attempt = reconnectAttempts.current++;  
      return Math.min(1000 * 2 ** attempt, 30000); // exponential backoff, cap 30s  
    },  
    onOpen: () => {  
      reconnectAttempts.current = 0;  
      console.info("[WS] Connected to Brain");  
      addLog("event", `Connected to Brain WebSocket`);  
    },  
    onClose: () => {  
      console.warn("[WS] Disconnected from Brain");  
      addLog("event", `Disconnected from Brain WebSocket`);  
    },  
    onError: (event) => {  
      console.error("[WS] Connection error:", event);  
      addLog("error", `WebSocket error: ${event.type || "unknown error"}`);  
    },  
  });  
  
  // Keep store updated with connection status  
  useEffect(() => {  
    setConnectionStatus(readyState);  
  }, [readyState, setConnectionStatus]);  
  
  // Forward incoming messages to store  
  useEffect(() => {  
    if (lastMessage?.data) {  
      handleWssMessage(lastMessage.data); // raw JSON string  
    }  
  }, [lastMessage, handleWssMessage]);  
}  