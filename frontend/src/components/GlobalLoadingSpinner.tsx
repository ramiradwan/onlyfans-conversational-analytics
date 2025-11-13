import React from 'react';  
import Backdrop from '@mui/material/Backdrop';  
import CircularProgress from '@mui/material/CircularProgress';  
import { useChatStore } from '../store/useChatStore';  
import { useTheme, alpha } from '@mui/material/styles';  
  
export function GlobalLoadingSpinner() {  
  const systemStatus = useChatStore(state => state.systemStatus);  
  const theme = useTheme();  
  
  const loading = systemStatus === 'PROCESSING_SNAPSHOT';  
  
  return (  
    <Backdrop  
      open={loading}  
      sx={{  
        zIndex: theme.zIndex.drawer + 1,  
        color: theme.palette.primary.contrastText,  
        bgcolor: alpha(theme.palette.common.black, 0.4),  
      }}  
    >  
      <CircularProgress color="primary" />  
    </Backdrop>  
  );  
}  