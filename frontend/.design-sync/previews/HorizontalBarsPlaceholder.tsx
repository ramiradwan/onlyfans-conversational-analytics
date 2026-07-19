import { Box, Typography } from '@mui/material';
import { HorizontalBarsPlaceholder } from 'onlyfans-analytics-frontend';

export function TopTopicsLoading() {
  return (
    <Box sx={{ bgcolor: 'background.paper', maxWidth: 560, p: 2 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Top topics by volume
      </Typography>
      <HorizontalBarsPlaceholder bars={5} />
    </Box>
  );
}
