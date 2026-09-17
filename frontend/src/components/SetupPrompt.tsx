import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import CheckRoundedIcon from '@mui/icons-material/CheckRounded';
import InsightsRoundedIcon from '@mui/icons-material/InsightsRounded';
import { Box, Button, Paper, Stack, Typography } from '@mui/material';
import { useId } from 'react';
import { Link as RouterLink } from 'react-router-dom';

import { VisuallyHidden } from './ui';

interface SetupPromptProps {
  /** Whether the browser extension is already connected, which completes the first step. */
  extensionConnected?: boolean;
  title: string;
}

function Step({ done, index, label }: { done: boolean; index: number; label: string }) {
  return (
    <Stack component="li" direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
      <Box
        aria-hidden="true"
        sx={(theme) => ({
          alignItems: 'center',
          bgcolor: done ? theme.vars.palette.success.main : 'transparent',
          border: done ? 'none' : `1.5px solid ${theme.vars.palette.divider}`,
          borderRadius: '50%',
          color: done ? theme.vars.palette.success.contrastText : theme.vars.palette.text.secondary,
          display: 'flex',
          flexShrink: 0,
          fontSize: '0.75rem',
          fontWeight: 600,
          height: 24,
          justifyContent: 'center',
          width: 24,
        })}
      >
        {done ? <CheckRoundedIcon sx={{ fontSize: 16 }} /> : index}
      </Box>
      <Typography
        variant="body2"
        sx={{ color: done ? 'text.secondary' : 'text.primary', fontWeight: done ? 400 : 500 }}
      >
        {label}
        {done && <VisuallyHidden> (done)</VisuallyHidden>}
      </Typography>
    </Stack>
  );
}

export function SetupPrompt({ extensionConnected = false, title }: SetupPromptProps) {
  const headingId = useId();
  return (
    <Paper
      data-visual="setup-prompt"
      component="section"
      aria-labelledby={headingId}
      sx={(theme) => ({
        alignSelf: 'center',
        maxWidth: 560,
        mx: 'auto',
        p: { xs: 3, sm: 4 },
        width: '100%',
        ...theme.effects.cardBorder(theme),
        ...theme.effects.ambientGlow(theme),
      })}
    >
      <Stack spacing={2.5} sx={{ alignItems: 'flex-start' }}>
        <Box
          aria-hidden="true"
          sx={(theme) => ({
            alignItems: 'center',
            bgcolor: theme.vars.palette.action.selected,
            borderRadius: `${theme.shape.borderRadius}px`,
            color: theme.vars.palette.primary.main,
            display: 'flex',
            height: 44,
            justifyContent: 'center',
            width: 44,
          })}
        >
          <InsightsRoundedIcon />
        </Box>
        <Box>
          <Typography component="h2" id={headingId} variant="h5">
            {title}
          </Typography>
          <Typography variant="body1" sx={{ color: 'text.secondary', mt: 1 }}>
            Two quick steps and your conversations show up here. Your conversation data stays on this computer.
          </Typography>
        </Box>
        <Stack component="ol" spacing={1.25} sx={{ listStyle: 'none', m: 0, p: 0 }}>
          <Step done={extensionConnected} index={1} label="Connect the browser extension" />
          <Step done={false} index={2} label="Turn on message history" />
        </Stack>
        <Button
          component={RouterLink}
          endIcon={<ArrowForwardIcon />}
          size="large"
          to="/settings"
          variant="contained"
        >
          Continue setup
        </Button>
      </Stack>
    </Paper>
  );
}
