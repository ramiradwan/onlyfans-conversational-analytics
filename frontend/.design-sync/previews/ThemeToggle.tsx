import { Box, Stack, Typography } from '@mui/material';
import { ThemeToggle } from 'onlyfans-analytics-frontend';

export function InApplicationBar() {
  return (
    <Stack
      alignItems="center"
      direction="row"
      justifyContent="space-between"
      sx={{
        bgcolor: 'background.paper',
        border: 1,
        borderColor: 'divider',
        borderRadius: 2,
        boxShadow: 1,
        minWidth: 360,
        p: 2,
      }}
    >
      <Box>
        <Typography fontWeight={600} variant="subtitle1">
          Bridge
        </Typography>
        <Typography color="text.secondary" variant="caption">
          Appearance follows your selected mode
        </Typography>
      </Box>
      <ThemeToggle />
    </Stack>
  );
}
