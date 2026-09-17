import { Box } from '@mui/material';
import { BrandMark } from 'onlyfans-analytics-frontend';

export function HeaderLockup() {
  return (
    <Box sx={{ bgcolor: 'background.paper', p: 2, width: 320 }}>
      <BrandMark />
    </Box>
  );
}
