import { Box, Stack, Typography } from '@mui/material';
import { ThemeToggle } from 'onlyfans-analytics-frontend';

export function InApplicationBar() {
  return (
    <Stack
      direction="row"
      sx={{
        alignItems: 'center',
        justifyContent: 'space-between',
        bgcolor: 'background.paper',
        border: 1,
        borderColor: 'divider',
        borderRadius: 2,
        boxShadow: 1,
        minWidth: 360,
        p: 2
      }}>
      <Box>
        <Typography variant="subtitle1" sx={{
          fontWeight: 600
        }}>
          Bridge
        </Typography>
        <Typography variant="caption" sx={{
          color: 'text.secondary'
        }}>
          Appearance follows your selected mode
        </Typography>
      </Box>
      <ThemeToggle />
    </Stack>
  );
}
