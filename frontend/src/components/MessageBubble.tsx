import { Box, Paper, Typography } from '@mui/material';
import React from 'react';
import { Message } from '@/types/backend'; // Import from auto-generated types

interface MessageBubbleProps {
  message: Message;
}

/**
 * Renders a single chat message bubble.
 * (Based on "Calm UI" Spec 2.0)
 *
 * - Differentiates between creator (outgoing) and fan (incoming).
 * - Uses token-driven colors: 'primary.main' for creator, 'background.paper' for fan.
 * - Uses token-driven layout: 'flex-end' for creator, 'flex-start' for fan.
 * - Uses 'elevation={0}' and borders for a "calm" (no shadow) appearance.
 */
export function MessageBubble({ message }: MessageBubbleProps) {
  // 'is_creator' field from the backend Message type determines alignment
  const isCreator = message.is_creator;

  return (
    <Box
      sx={{
        display: 'flex',
        justifyContent: isCreator ? 'flex-end' : 'flex-start',
        mb: 2,
      }}
    >
      <Paper
        elevation={0} // "Calm UI" - no shadows
        sx={{
          py: 1.5,
          px: 2,
          // Use theme's border radius token
          borderRadius: (theme) => theme.shape.borderRadius,
          // TOKEN-DRIVEN: Differentiate sender/receiver
          bgcolor: isCreator ? 'primary.main' : 'background.paper',
          color: isCreator ? 'primary.contrastText' : 'text.primary',
          // Use 'divider' token for border on non-creator messages
          border: (theme) =>
            isCreator ? 'none' : `1px solid ${theme.vars.palette.divider}`,
          maxWidth: '80%',
        }}
      >
        <Typography variant="body1" sx={{ wordBreak: 'break-word' }}>
          {message.text}
        </Typography>
        <Typography
          variant="caption"
          sx={{
            display: 'block',
            textAlign: 'right',
            opacity: 0.7,
            mt: 0.5,
          }}
        >
          {/* Format the ISO date string */}
          {message.createdAt
            ? new Date(message.createdAt).toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit',
              })
            : ''}
        </Typography>
      </Paper>
    </Box>
  );
}