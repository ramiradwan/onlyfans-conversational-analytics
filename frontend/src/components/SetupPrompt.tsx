import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import CheckRoundedIcon from '@mui/icons-material/CheckRounded';
import InsightsRoundedIcon from '@mui/icons-material/InsightsRounded';
import { Box, Button, Paper, Stack, Typography } from '@mui/material';
import { keyframes, type Theme } from '@mui/material/styles';
import { useId, useState } from 'react';
import { Link as RouterLink } from 'react-router-dom';

import { effectTokens } from '../theme';
import { VisuallyHidden } from './ui';

interface SetupPromptProps {
  /** Whether the browser extension is already connected, which completes the first step. */
  extensionConnected?: boolean;
  /** Whether message-history consent has been given and syncing has started. */
  historyEnabled?: boolean;
  /** Whether Full analytics can actually admit licensed analysis. */
  fullAnalyticsReady?: boolean;
  title: string;
}

type StepState = 'done' | 'current' | 'upcoming';

const checkEnter = keyframes`
  from {
    opacity: 0;
    transform: scale(0.6);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
`;

/** Plays the check animation only when a step completes while the prompt is shown. */
function useJustCompleted(done: boolean): boolean {
  const [previous, setPrevious] = useState(done);
  const [justCompleted, setJustCompleted] = useState(false);
  if (done !== previous) {
    setPrevious(done);
    setJustCompleted(done);
  }
  return justCompleted;
}

function Step({ index, label, state }: { index: number; label: string; state: StepState }) {
  const done = state === 'done';
  const current = state === 'current';
  const justCompleted = useJustCompleted(done);
  return (
    <Stack
      aria-current={current ? 'step' : undefined}
      component="li"
      data-step-state={state}
      direction="row"
      spacing={1.5}
      sx={{ alignItems: 'center' }}
    >
      <Box
        aria-hidden="true"
        sx={(theme) => ({
          alignItems: 'center',
          bgcolor: done
            ? theme.vars.palette.success.main
            : current
              ? theme.vars.palette.action.selected
              : 'transparent',
          border: done ? 'none' : `1.5px solid ${current ? theme.vars.palette.primary.main : theme.vars.palette.divider}`,
          borderRadius: '50%',
          color: done
            ? theme.vars.palette.success.contrastText
            : current
              ? theme.vars.palette.primary.main
              : theme.vars.palette.text.secondary,
          display: 'flex',
          flexShrink: 0,
          fontSize: '0.75rem',
          fontWeight: 600,
          height: 24,
          justifyContent: 'center',
          transition: transitionFor(theme, ['background-color', 'border-color', 'color']),
          width: 24,
        })}
      >
        {done ? (
          <CheckRoundedIcon
            sx={{
              animation: justCompleted
                ? `${checkEnter} ${effectTokens.motion.duration.standard} ${effectTokens.motion.easing.enter} both`
                : 'none',
              fontSize: 16,
            }}
          />
        ) : (
          index
        )}
      </Box>
      <Typography
        variant="body2"
        sx={{ color: current ? 'text.primary' : 'text.secondary', fontWeight: current ? 500 : 400 }}
      >
        {label}
        {done && <VisuallyHidden> (done)</VisuallyHidden>}
      </Typography>
    </Stack>
  );
}

function transitionFor(theme: Theme, properties: string[]): string {
  return theme.transitions.create(properties, { duration: theme.transitions.duration.standard });
}

export function SetupPrompt({
  extensionConnected = false,
  historyEnabled = false,
  fullAnalyticsReady = false,
  title,
}: SetupPromptProps) {
  const headingId = useId();
  const steps = [
    { done: extensionConnected, label: 'Connect the browser extension' },
    { done: historyEnabled, label: 'Turn on message history' },
    { done: fullAnalyticsReady, label: 'Turn on Full analytics' },
  ];
  const completed = steps.filter((step) => step.done).length;
  const currentIndex = steps.findIndex((step) => !step.done);
  return (
    <Paper
      data-journey-state="desktop.setup_prompt"
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
            Your conversations start syncing after the first two steps. Full analytics is ready after all three.
            Synced message history stays on this computer.
          </Typography>
        </Box>
        <Typography variant="caption" sx={{ color: 'text.secondary', fontWeight: 600 }}>
          {completed} of {steps.length} complete
        </Typography>
        <Stack component="ol" spacing={1.25} sx={{ listStyle: 'none', m: 0, p: 0 }}>
          {steps.map((step, index) => (
            <Step
              key={step.label}
              index={index + 1}
              label={step.label}
              state={step.done ? 'done' : index === currentIndex ? 'current' : 'upcoming'}
            />
          ))}
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
