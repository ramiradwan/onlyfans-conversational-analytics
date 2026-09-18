import {
  Box,
  Grid,
  KpiPlaceholder,
  Typography,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function DashboardMetricsLoading() {
  return (
    <Box sx={{ width: '100%' }}>
      <Typography
        variant="subtitle2"
        sx={{
          color: 'text.secondary',
          mb: 1.5
        }}>
        Processing your data…
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
