import AccountCircleIcon from '@mui/icons-material/AccountCircle';  
import ExtensionIcon from '@mui/icons-material/Extension';  
import MenuIcon from '@mui/icons-material/Menu';  
import {  
  AppBar,  
  Toolbar,  
  Typography,  
  IconButton,  
  Badge,  
  Tooltip,  
  Stack,  
  useTheme,  
} from '@mui/material';  
import React from 'react';  
import { ThemeToggle } from '@/components/ThemeToggle';  
import {  
  useBackendConnectionState,  
  useExtensionConnectionState,  
  useSystemStatus,  
} from '@store/systemStore';  
  
const statusColorMap = {  
  connected: 'success',  
  connecting: 'warning',  
  disconnected: 'error',  
  error: 'error',  
} as const;  
  
const humanStatusMap: Record<string, string> = {  
  connected: 'Connected to backend',  
  connecting: 'Connecting to backend…',  
  disconnected: 'Disconnected from backend',  
  error: 'Backend connection error',  
};  
  
interface AppAppBarProps {  
  drawerWidth: number;  
  onDrawerToggle: () => void;  
}  
  
export function AppAppBar({ drawerWidth, onDrawerToggle }: AppAppBarProps) {  
  const theme = useTheme();  
  const backendState = useBackendConnectionState();  
  const extensionState = useExtensionConnectionState();  
  const [status, detail] = useSystemStatus();  
  
  const backendColor = statusColorMap[backendState];  
  const extensionColor = statusColorMap[extensionState];  
  
  const backendTooltip = detail || humanStatusMap[backendState] || status;  
  const extensionTooltip = `Extension: ${extensionState}`;  
  
  return (  
    <AppBar  
      position="fixed"  
      sx={{  
        bgcolor: theme.vars.palette.background.paper,  
        color: theme.vars.palette.text.primary,  
        boxShadow: 'none',  
        ...theme.effects.headerBorder(theme),  
        width: { sm: `calc(100% - ${drawerWidth}px)` },  
        ml: { sm: `${drawerWidth}px` },  
      }}  
    >  
      <Toolbar>  
        <IconButton  
          color="inherit"  
          aria-label="open drawer"  
          edge="start"  
          onClick={onDrawerToggle}  
          sx={{ mr: 2, display: { sm: 'none' } }}  
        >  
          <MenuIcon />  
        </IconButton>  
  
        <Typography variant="h6" noWrap sx={{ flexGrow: 1 }}>  
          OnlyFans Analytics  
        </Typography>  
  
        <Stack direction="row" spacing={1}>  
          {/* Backend connection indicator */}  
          <Tooltip title={backendTooltip}>  
            <Badge  
              variant="dot"  
              color={backendColor}  
              anchorOrigin={{  
                vertical: 'bottom',  
                horizontal: 'right',  
              }}  
              sx={{ '& .MuiBadge-dot': { height: 10, width: 10 } }}  
            >  
              <IconButton  
                color="inherit"  
                aria-live="polite"  
                sx={{  
                  '&:hover': { bgcolor: theme.vars.palette.action.hover },  
                  borderRadius: 2,  
                }}  
              >  
                <AccountCircleIcon />  
              </IconButton>  
            </Badge>  
          </Tooltip>  
  
          {/* Extension connection indicator */}  
          <Tooltip title={extensionTooltip}>  
            <Badge  
              variant="dot"  
              color={extensionColor}  
              anchorOrigin={{  
                vertical: 'bottom',  
                horizontal: 'right',  
              }}  
              sx={{ '& .MuiBadge-dot': { height: 10, width: 10 } }}  
            >  
              <IconButton  
                color="inherit"  
                sx={{  
                  '&:hover': { bgcolor: theme.vars.palette.action.hover },  
                  borderRadius: 2,  
                }}  
              >  
                <ExtensionIcon />  
              </IconButton>  
            </Badge>  
          </Tooltip>  
  
          {/* Theme toggle */}  
          <ThemeToggle />  
        </Stack>  
      </Toolbar>  
    </AppBar>  
  );  
}  