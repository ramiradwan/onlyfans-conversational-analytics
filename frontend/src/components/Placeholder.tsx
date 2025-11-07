import React from "react";  
import { Box, Typography } from "@mui/material";  
import ChatBubbleOutlineIcon from "@mui/icons-material/ChatBubbleOutline";  
  
interface PlaceholderProps {  
  title: string;  
  subtitle?: string;  
  icon?: React.ReactNode;  
}  
  
/**  
 * Placeholder  
 * -----------  
 * Renders a centered empty-state message with optional subtitle and icon.  
 * Used for "no data yet", "loading", or "please select" states.  
 */  
export default function Placeholder({  
  title,  
  subtitle,  
  icon,  
}: PlaceholderProps) {  
  return (  
    <Box  
      sx={{  
        display: "flex",  
        flexDirection: "column",  
        alignItems: "center",  
        justifyContent: "center",  
        textAlign: "center",  
        py: 4,  
        px: 2,  
        color: "text.secondary",  
      }}  
    >  
      <Box sx={{ fontSize: 48, mb: 1 }}>  
        {icon || <ChatBubbleOutlineIcon fontSize="inherit" />}  
      </Box>  
      <Typography variant="h6" gutterBottom>  
        {title}  
      </Typography>  
      {subtitle && (  
        <Typography variant="body2" color="text.secondary">  
          {subtitle}  
        </Typography>  
      )}  
    </Box>  
  );  
}  