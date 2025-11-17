import { Typography, useTheme } from '@mui/material';  
import React from 'react';  
  
export type AsyncContentProps<T> = {  
  isLoading: boolean;  
  data: T[] | null | undefined;  
  placeholder: React.ReactNode;  
  emptyMessage: React.ReactNode; // changed from string  
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
      <>  
        {typeof emptyMessage === 'string' ? (  
          <Typography  
            color={theme.vars.palette.text.secondary}  
            align="center"  
            sx={{ mt: 4 }}  
          >  
            {emptyMessage}  
          </Typography>  
        ) : (  
          emptyMessage  
        )}  
      </>  
    );  
  }  
  
  return <>{render(data)}</>;  
}  