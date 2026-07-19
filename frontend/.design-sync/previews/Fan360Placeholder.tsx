import { Box, Typography } from '@mui/material';
import { Fan360Placeholder } from 'onlyfans-analytics-frontend';

export function FanProfileLoading() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 420, p: 3 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Fan 360
      </Typography>
      <Fan360Placeholder />
    </Box>
  );
}
