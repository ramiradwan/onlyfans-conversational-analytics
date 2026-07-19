import { Box } from '@mui/material';
import { GlobalLoader } from 'onlyfans-analytics-frontend';

export function ProcessingOverlay() {
  return (
    <Box sx={{ bgcolor: 'background.default', minHeight: 360, position: 'relative' }}>
      <GlobalLoader />
    </Box>
  );
}
