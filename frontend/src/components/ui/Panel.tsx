import { Paper, useTheme, SxProps } from '@mui/material';  
import React from 'react';  
  
export const Panel: React.FC<{ children: React.ReactNode; sx?: SxProps }> = ({  
  children,  
  sx,  
}) => {  
  const theme = useTheme();  
  return (  
    <Paper  
      sx={{  
        p: 3,  
        bgcolor: theme.vars.palette.background.paper,  
        display: 'flex',  
        flexDirection: 'column',  
        gap: 2,  
        ...theme.effects.cardBorder(theme),  
        ...sx,  
      }}  
      elevation={0}  
    >  
      {children}  
    </Paper>  
  );  
};  