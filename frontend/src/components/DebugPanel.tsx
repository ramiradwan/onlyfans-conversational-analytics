import React from "react";  
import {  
  Box,  
  Typography,  
  List,  
  ListItem,  
  ListItemText,  
  Paper,  
  useTheme,  
  Divider,  
  IconButton,  
  Tooltip,  
  ToggleButtonGroup,  
  ToggleButton,  
  useMediaQuery,  
} from "@mui/material";  
import PauseIcon from "@mui/icons-material/Pause";  
import PlayArrowIcon from "@mui/icons-material/PlayArrow";  
import Placeholder from "./Placeholder";  
import type { LogSeverity, DebugLog } from "../store/useDebugStore";  
import { useDebugStore } from "../store/useDebugStore";  
  
export default function DebugPanel() {  
  const theme = useTheme();  
  const containerRef = React.useRef<HTMLDivElement>(null);  
  
  const logs = useDebugStore((s) => s.getLogs());  
  
  const [autoScroll, setAutoScroll] = React.useState(true);  
  const [filter, setFilter] = React.useState<LogSeverity | "all">("all");  
  const isMobile = useMediaQuery(theme.breakpoints.down("md"));  
  
  React.useEffect(() => {  
    if (autoScroll && containerRef.current) {  
      containerRef.current.scrollTop = containerRef.current.scrollHeight;  
    }  
  }, [logs, autoScroll]);  
  
  React.useEffect(() => {  
    if (containerRef.current) {  
      containerRef.current.focus();  
    }  
  }, []);  
  
  const parseLogColor = (severity: LogSeverity) => {  
    switch (severity) {  
      case "error":  
        return theme.palette.error.main;  
      case "warn":  
        return theme.palette.warning.main;  
      case "event":  
        return theme.palette.info.main;  
      default:  
        return theme.palette.text.primary;  
    }  
  };  
  
  const severityLabel = (severity: LogSeverity) => {  
    switch (severity) {  
      case "error":  
        return "[ERROR]";  
      case "warn":  
        return "[WARN]";  
      case "event":  
        return "[EVENT]";  
      default:  
        return "[INFO]";  
    }  
  };  
  
  const filteredLogs =  
    filter === "all" ? logs : logs.filter((l: DebugLog) => l.severity === filter);  
  
  const content = (  
    <>  
      <Box sx={{ display: "flex", alignItems: "center", mb: 1 }}>  
        <Typography  
          variant="subtitle2"  
          sx={{ flexGrow: 1, color: theme.palette.text.secondary }}  
        >  
          Debug Log ({filteredLogs.length})  
        </Typography>  
        <Tooltip title={autoScroll ? "Pause auto-scroll" : "Resume auto-scroll"}>  
          <IconButton  
            size="small"  
            aria-label={autoScroll ? "Pause auto-scroll" : "Resume auto-scroll"}  
            onClick={() => setAutoScroll((prev) => !prev)}  
          >  
            {autoScroll ? <PauseIcon /> : <PlayArrowIcon />}  
          </IconButton>  
        </Tooltip>  
      </Box>  
  
      <ToggleButtonGroup  
        size="small"  
        value={filter}  
        exclusive  
        onChange={(_, val) => val && setFilter(val)}  
        sx={{ mb: 1 }}  
        aria-label="Log severity filter"  
      >  
        <ToggleButton value="all">All</ToggleButton>  
        <ToggleButton value="info">Info</ToggleButton>  
        <ToggleButton value="warn">Warn</ToggleButton>  
        <ToggleButton value="error">Error</ToggleButton>  
        <ToggleButton value="event">Event</ToggleButton>  
      </ToggleButtonGroup>  
  
      <Divider sx={{ mb: 1 }} />  
  
      {filteredLogs.length === 0 ? (  
        <Placeholder title="No logs yet" subtitle="Activity will appear here" />  
      ) : (  
        <List  
          dense  
          role="log"  
          aria-live="polite"  
          sx={{ transition: "all 0.2s ease" }}  
          onScroll={(e) => {  
            const target = e.currentTarget;  
            const atBottom =  
              target.scrollHeight - target.scrollTop <= target.clientHeight + 5;  
            setAutoScroll(atBottom);  
          }}  
        >  
          {filteredLogs.map((log: DebugLog, idx: number) => (  
            <ListItem  
              key={`${log.timestamp}-${idx}`}  
              disableGutters  
              sx={{  
                bgcolor:  
                  idx % 2 === 0  
                    ? theme.palette.background.default  
                    : theme.palette.action.hover,  
                py: 0.5,  
                px: 1,  
              }}  
            >  
              <ListItemText  
                primary={  
                  <Box sx={{ display: "flex", justifyContent: "space-between" }}>  
                    <Typography  
                      component="span"  
                      sx={{  
                        fontFamily: "monospace",  
                        fontSize: theme.typography.caption.fontSize,  
                        color: parseLogColor(log.severity),  
                        fontStyle: log.severity === "event" ? "italic" : "normal",  
                      }}  
                    >  
                      {`${severityLabel(log.severity)} ${log.message}`}  
                    </Typography>  
                    <Typography  
                      component="span"  
                      sx={{  
                        fontSize: theme.typography.caption.fontSize,  
                        color: theme.palette.text.disabled,  
                        ml: 1,  
                      }}  
                    >  
                      {log.timestamp}  
                    </Typography>  
                  </Box>  
                }  
              />  
            </ListItem>  
          ))}  
        </List>  
      )}  
    </>  
  );  
  
  const baseStyles = {  
    p: 2,  
    bgcolor: theme.palette.background.paper,  
    borderRadius: theme.shape.borderRadius,  
    boxShadow: theme.shadows[4],  
    overflowY: "auto",  
  };  
  
  if (isMobile) {  
    return (  
      <Box  
        ref={containerRef}  
        tabIndex={0}  
        sx={{  
          position: "fixed",  
          bottom: 0,  
          left: 0,  
          width: "100%",  
          maxHeight: "40vh",  
          borderTop: `1px solid ${theme.palette.divider}`,  
          borderTopLeftRadius: theme.shape.borderRadius,  
          borderTopRightRadius: theme.shape.borderRadius,  
          zIndex: theme.zIndex.modal,  
          ...baseStyles,  
        }}  
      >  
        {content}  
      </Box>  
    );  
  }  
  
  return (  
    <Paper  
      ref={containerRef}  
      tabIndex={0}  
      elevation={1}  
      sx={{  
        height: "100%",  
        border: `1px solid ${theme.palette.divider}`,  
        ...baseStyles,  
      }}  
    >  
      {content}  
    </Paper>  
  );  
}  