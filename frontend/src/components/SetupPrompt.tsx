import ArrowForwardIcon from '@mui/icons-material/ArrowForward';
import { Button, Stack, Typography } from '@mui/material';
import { Link as RouterLink } from 'react-router-dom';

import { Panel } from './ui';

interface SetupPromptProps {
  title: string;
}

export function SetupPrompt({ title }: SetupPromptProps) {
  return (
    <Panel>
      <Stack spacing={1.5} sx={{ alignItems: 'flex-start' }}>
        <Typography component="h2" variant="h6">
          {title}
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary', maxWidth: 560 }}>
          Connect the browser extension and start syncing your message history. Everything stays
          on this computer.
        </Typography>
        <Button
          component={RouterLink}
          endIcon={<ArrowForwardIcon />}
          to="/settings"
          variant="contained"
        >
          Continue setup
        </Button>
      </Stack>
    </Panel>
  );
}
