import { Box, CircularProgress, Typography, Backdrop } from '@mui/material';
import React from 'react';
// The 'lottie-react' package is in your package.json,
// you would import it here once you have a Lottie JSON file.
// import Lottie from "lottie-react";
// import loadingAnimation from "@assets/loading-animation.json";

/**
 * Full-screen loading overlay.
 * Uses "Glassmorphism" effect (Spec 13.0) via backdropFilter.
 *
 * This component will be shown by:
 * 1. React.Suspense (for lazy-loading pages)
 * 2. websocketService (when 'system_status' is 'PROCESSING_SNAPSHOT')
 */
export function GlobalLoader() {
  return (
    <Backdrop
      open={true}
      sx={{
        // Ensure it's above other UI elements
        zIndex: (theme) => theme.zIndex.drawer + 1,
        
        // TOKEN-DRIVEN: Glassmorphism effect (Spec 13.0)
        // This creates the "frosted glass" look
        backdropFilter: 'blur(5px)',
        
        // Apply a semi-transparent overlay
        // We use 'theme.vars' for correct dark/light mode
        backgroundColor: (theme) =>
          theme.palette.mode === 'dark'
            ? 'rgba(0, 0, 0, 0.3)'
            : 'rgba(255, 255, 255, 0.3)',
      }}
    >
      <Box
        sx={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'text.primary',
        }}
      >
        {/* // LOTTIE PLACEHOLDER (Spec 13.0)
          // Once you have 'loading-animation.json', replace
          // <CircularProgress> with <Lottie>
          
          <Lottie 
            animationData={loadingAnimation} 
            loop={true} 
            style={{ width: 150, height: 150 }}
          />
        */}
        <CircularProgress color="primary" />
        <Typography variant="h6" sx={{ mt: 2 }}>
          Processing Data...
        </Typography>
      </Box>
    </Backdrop>
  );
}