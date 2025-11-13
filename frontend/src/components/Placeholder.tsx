import React from "react";  
import { Box, Typography, useTheme, Paper, Button } from "@mui/material";  
import ChatBubbleOutlineIcon from "@mui/icons-material/ChatBubbleOutline";  
  
interface PlaceholderProps {  
  title: string;  
  subtitle?: string;  
  icon?: React.ReactNode;  
  actionLabel?: string;  
  onAction?: () => void;  
}  
  
export default function Placeholder({  
  title,  
  subtitle,  
  icon,  
  actionLabel,  
  onAction,  
}: PlaceholderProps) {  
  const theme = useTheme();  
  
  return (  
    <Box  
      role="status"  
      aria-live="polite"  
      sx={{  
        display: "flex",  
        flexDirection: "column",  
        alignItems: "center",  
        justifyContent: "center",  
        textAlign: "center",  
        py: theme.spacing(4),  
        px: theme.spacing(2),  
        color: "text.secondary",  
      }}  
    >  
      {/* Icon wrapper with calm background */}  
      <Paper  
        elevation={0}  
        sx={{  
          bgcolor: theme.palette.action.hover,  
          borderRadius: "50%",  
          width: { xs: 64, md: 80 },  
          height: { xs: 64, md: 80 },  
          display: "flex",  
          alignItems: "center",  
          justifyContent: "center",  
          mb: theme.spacing(2),  
        }}  
        aria-hidden="true" // explicitly mark as decorative  
      >  
        {icon || <ChatBubbleOutlineIcon sx={{ fontSize: { xs: 32, md: 40 } }} />}  
      </Paper>  
  
      <Typography variant="h6" gutterBottom>  
        {title}  
      </Typography>  
  
      {subtitle && (  
        <Typography  
          variant="body2"  
          color="text.secondary"  
          sx={{ mb: actionLabel ? 2 : 0 }}  
        >  
          {subtitle}  
        </Typography>  
      )}  
  
      {actionLabel && onAction && (  
        <Button  
          variant="outlined"  
          size="small"  
          onClick={onAction}  
          aria-label={actionLabel} // extra accessibility  
        >  
          {actionLabel}  
        </Button>  
      )}  
    </Box>  
  );  
}  