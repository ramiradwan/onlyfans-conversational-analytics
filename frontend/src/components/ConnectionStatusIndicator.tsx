import React from "react";  
import { Tooltip, Box } from "@mui/material";  
import { useTheme } from "@mui/material/styles";  
import { ReadyState } from "react-use-websocket";  
import { useChatStore } from "../store/useChatStore";  
  
export default function ConnectionStatusIndicator() {  
  const theme = useTheme();  
  const readyState = useChatStore((s) => s.getReadyState());  
  
  const statusMap: Record<number, { label: string; color: string }> = {  
    [ReadyState.CONNECTING]: { label: "Connecting", color: theme.palette.warning.main },  
    [ReadyState.OPEN]: { label: "Connected", color: theme.palette.success.main },  
    [ReadyState.CLOSING]: { label: "Closing", color: theme.palette.warning.dark },  
    [ReadyState.CLOSED]: { label: "Disconnected", color: theme.palette.error.main },  
    [ReadyState.UNINSTANTIATED]: { label: "Uninstantiated", color: theme.palette.grey[500] },  
  };  
  
  const { label, color } =  
    statusMap[readyState] || statusMap[ReadyState.UNINSTANTIATED];  
  
  return (  
    <Tooltip title={`WS Status: ${label}`} arrow>  
      <Box  
        role="status"  
        aria-label={`WebSocket status: ${label}`}  
        sx={{  
          width: 12,  
          height: 12,  
          borderRadius: "50%",  
          bgcolor: color,  
          ml: 2,  
        }}  
      />  
    </Tooltip>  
  );  
}  