import React from "react";  
import { Box, Typography, List, ListItem, ListItemText, Paper, useTheme } from "@mui/material";  
  
interface DebugPanelProps {  
  logs: string[];  
  visible: boolean;  
  isMobile?: boolean;  
}  
  
/**  
 * DebugPanel  
 * ----------  
 * Displays logs in a scrollable panel.  
 * No data shaping, purely presentational.  
 */  
export default function DebugPanel({ logs, visible, isMobile = false }: DebugPanelProps) {  
  const theme = useTheme();  
  const containerRef = React.useRef<HTMLDivElement>(null);  
  
  React.useEffect(() => {  
    if (visible && containerRef.current) {  
      containerRef.current.scrollTop = containerRef.current.scrollHeight;  
    }  
  }, [logs, visible]);  
  
  if (!visible) return null;  
  
  const content = (  
    <>  
      <Typography variant="subtitle2" sx={{ mb: 1, color: theme.palette.grey[100] }}>  
        Debug Log  
      </Typography>  
      <List dense>  
        {logs.map((log, idx) => (  
          <ListItem key={idx} disableGutters>  
            <ListItemText  
              primary={log}  
              primaryTypographyProps={{  
                fontFamily: "monospace",  
                fontSize: "0.75rem",  
                color: theme.palette.grey[100],  
              }}  
            />  
          </ListItem>  
        ))}  
      </List>  
    </>  
  );  
  
  if (isMobile) {  
    return (  
      <Box  
        ref={containerRef}  
        sx={{  
          display: { xs: "block", md: "none" },  
          position: "fixed",  
          bottom: 0,  
          left: 0,  
          width: "100%",  
          height: "30vh",  
          bgcolor: theme.palette.grey[900],  
          borderTop: 1,  
          borderColor: theme.palette.grey[800],  
          overflowY: "auto",  
          p: 1.5,  
          zIndex: 1200,  
        }}  
      >  
        {content}  
      </Box>  
    );  
  }  
  
  return (  
    <Paper  
      ref={containerRef}  
      sx={{  
        p: 1.5,  
        bgcolor: theme.palette.grey[900],  
        border: 1,  
        borderColor: theme.palette.grey[800],  
        borderRadius: 1,  
        boxShadow: theme.shadows[2],  
        overflowY: "auto",  
        height: "100%",  
      }}  
    >  
      {content}  
    </Paper>  
  );  
}  