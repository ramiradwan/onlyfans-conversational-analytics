import { Box, QueryInput, Typography } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function Ready() {
  return (
    <Box sx={{ maxWidth: 720 }}>
      <Typography
        variant="subtitle2"
        sx={{
          color: 'text.secondary',
          mb: 1
        }}>
        Explore audience data
      </Typography>
      <QueryInput onSend={() => {}} />
    </Box>
  );
}

export function DisabledWhileProcessing() {
  return (
    <Box sx={{ maxWidth: 720 }}>
      <Typography
        variant="subtitle2"
        sx={{
          color: 'text.secondary',
          mb: 1
        }}>
        Generating an answer…
      </Typography>
      <QueryInput onSend={() => {}} disabled />
    </Box>
  );
}
