import { Backdrop, Box, CircularProgress, Typography } from '@mui/material';

/**
 * Full-screen loading overlay using the design system glassmorphism effect.
 */
export function GlobalLoader() {
  return (
    <Backdrop
      open
      sx={(theme) => ({
        zIndex: theme.zIndex.drawer + 1,
        ...theme.effects.glassmorphism(theme),
      })}
    >
      <Box
        sx={{
          alignItems: 'center',
          color: 'text.primary',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
        }}
      >
        <CircularProgress color="primary" />
        <Typography variant="h6" sx={{ mt: 2 }}>
          Processing Data...
        </Typography>
      </Box>
    </Backdrop>
  );
}
