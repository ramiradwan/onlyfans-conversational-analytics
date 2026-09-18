import {
  Box,
  Stack,
  ThemeToggle,
  Typography,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

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
          Appearance
        </Typography>
        <Typography variant="caption" sx={{
          color: 'text.secondary'
        }}>
          Switch between light and dark
        </Typography>
      </Box>
      <ThemeToggle />
    </Stack>
  );
}
