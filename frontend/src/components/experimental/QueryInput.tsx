import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';  
import SendIcon from '@mui/icons-material/Send';  
import { Paper, TextField, IconButton } from '@mui/material';  
import React, { useState } from 'react';  
  
interface QueryInputProps {  
  onSend: (text: string) => void;  
  disabled?: boolean;  
}  
  
export function QueryInput({ onSend, disabled = false }: QueryInputProps) {  
  const [text, setText] = useState('');  
  
  const handleSubmit = (e: React.FormEvent) => {  
    e.preventDefault();  
    if (text.trim()) {  
      onSend(text.trim());  
      setText('');  
    }  
  };  
  
  return (  
    <Paper  
      component="form"  
      onSubmit={handleSubmit}  
      elevation={0}  
      sx={{  
        p: 1,  
        display: 'flex',  
        alignItems: 'center',  
        bgcolor: 'background.paper', // "chrome"  
        borderTop: (theme) => `1px solid ${theme.vars.palette.divider}`,  
      }}  
    >  
      <AutoAwesomeIcon sx={{ color: 'text.disabled', mx: 1 }} />  
      <TextField  
        fullWidth  
        variant="outlined"  
        placeholder="Ask a question... (e.g., 'Who are my top 10 fans?')"  
        value={text}  
        onChange={(e) => setText(e.target.value)}  
        disabled={disabled}  
        multiline  
        maxRows={4}  
        sx={{  
          '& .MuiOutlinedInput-root': {  
            '& fieldset': { border: 'none' }, // Calm UI  
          },  
        }}  
      />  
      <IconButton  
        type="submit"  
        color="primary"  
        disabled={disabled || !text.trim()}  
      >  
        <SendIcon />  
      </IconButton>  
    </Paper>  
  );  
}  