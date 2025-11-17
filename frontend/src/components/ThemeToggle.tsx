// src/components/ThemeToggle.tsx  
import * as React from 'react';  
import { useColorScheme } from '@mui/material/styles';  
import { IconButton, Tooltip } from '@mui/material';  
import DarkModeIcon from '@mui/icons-material/DarkMode';  
import LightModeIcon from '@mui/icons-material/LightMode';  
import MonitorIcon from '@mui/icons-material/Monitor';  
  
export function ThemeToggle() {  
  const { mode, setMode } = useColorScheme();  
  const [mounted, setMounted] = React.useState(false);  
  
  React.useEffect(() => {  
    setMounted(true);  
  }, []);  
  
  if (!mounted || !mode) return null;  
  
  const applyHtmlClass = (newMode: 'light' | 'dark' | 'system') => {  
    const html = document.documentElement;  
    html.classList.remove('light', 'dark');  
    if (newMode === 'light' || newMode === 'dark') {  
      html.classList.add(newMode);  
    } else {  
      // system mode: remove explicit class so OS preference applies  
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;  
      html.classList.add(prefersDark ? 'dark' : 'light');  
    }  
  };  
  
  let icon: React.ReactNode;  
  let label: string;  
  switch (mode) {  
    case 'light':  
      icon = <DarkModeIcon />;  
      label = 'Switch to dark mode';  
      break;  
    case 'dark':  
      icon = <LightModeIcon />;  
      label = 'Switch to system mode';  
      break;  
    case 'system':  
    default:  
      icon = <MonitorIcon />;  
      label = 'Switch to light mode';  
      break;  
  }  
  
  const handleToggle = () => {  
    const nextMode =  
      mode === 'light' ? 'dark' : mode === 'dark' ? 'system' : 'light';  
    setMode(nextMode);  
    applyHtmlClass(nextMode); // manual sync to <html> class  
  };  
  
  return (  
    <Tooltip title={label}>  
      <IconButton onClick={handleToggle} aria-label={label} color="inherit">  
        {icon}  
      </IconButton>  
    </Tooltip>  
  );  
}  