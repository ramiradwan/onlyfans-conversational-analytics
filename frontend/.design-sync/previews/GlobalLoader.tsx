import { Box, GlobalLoader } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function ProcessingOverlay() {
  return (
    <Box sx={{ minHeight: 360, position: 'relative' }}>
      <GlobalLoader />
    </Box>
  );
}
