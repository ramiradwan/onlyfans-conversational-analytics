import { Box, Grid, Typography } from '@mui/material';
import { KpiPlaceholder } from 'onlyfans-analytics-frontend';

export function DashboardMetricsLoading() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2, width: '100%' }}>
      <Typography
        variant="subtitle2"
        sx={{
          color: 'text.secondary',
          mb: 1.5
        }}>
        Loading key metrics
      </Typography>
      <Grid container spacing={2}>
        {Array.from({ length: 4 }, (_, index) => (
          <Grid key={index} size={{ xs: 12, sm: 6, md: 3 }}>
            <KpiPlaceholder />
          </Grid>
        ))}
      </Grid>
    </Box>
  );
}
