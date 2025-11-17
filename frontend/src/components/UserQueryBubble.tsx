import React from 'react';  
import { Paper, Typography, Box } from '@mui/material';  
import { styled } from '@mui/material/styles';  
  
const Bubble = styled(Paper)(({ theme }) => ({  
  padding: theme.spacing(1, 1.5),  
  borderRadius: theme.shape.borderRadius,  
  maxWidth: '70%',  
  wordBreak: 'break-word',  
  boxShadow: 'none',  
  backgroundColor: theme.vars.palette.primary.main,  
  color: theme.vars.palette.primary.contrastText,  
  borderBottomRightRadius: 0,  
}));  
  
export function UserQueryBubble({ text }: { text: string }) {  
  return (  
    <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>  
      <Bubble>  
        <Typography variant="body1">{text}</Typography>  
      </Bubble>  
    </Box>  
  );  
}  