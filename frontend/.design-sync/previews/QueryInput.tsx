import { Box, Typography } from '@mui/material';
import { QueryInput } from 'onlyfans-analytics-frontend';

export function Ready() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 720, p: 2 }}>
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 1 }}>
        Explore audience data
      </Typography>
      <QueryInput onSend={() => {}} />
    </Box>
  );
}

export function DisabledWhileProcessing() {
  return (
    <Box sx={{ bgcolor: 'background.default', maxWidth: 720, p: 2 }}>
      <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 1 }}>
        Generating an answer…
      </Typography>
      <QueryInput onSend={() => {}} disabled />
    </Box>
  );
}
