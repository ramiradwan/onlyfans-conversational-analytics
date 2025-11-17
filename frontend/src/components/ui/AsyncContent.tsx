import React from 'react';  
import { Typography, useTheme } from '@mui/material';  
  
export type AsyncContentProps<T> = {  
  isLoading: boolean;  
  data: T[] | null | undefined;  
  placeholder: React.ReactNode;  
  emptyMessage: string;  
  render: (data: T[]) => React.ReactNode;  
};  
  
export function AsyncContent<T>({  
  isLoading,  
  data,  
  placeholder,  
  emptyMessage,  
  render,  
}: AsyncContentProps<T>) {  
  const theme = useTheme();  
  
  if (isLoading) return <>{placeholder}</>;  
  if (!data || data.length === 0) {  
    return (  
      <Typography  
        color={theme.vars.palette.text.secondary}  
        align="center"  
        sx={{ mt: 4 }}  
      >  
        {emptyMessage}  
      </Typography>  
    );  
  }  
  return <>{render(data)}</>;  
}  