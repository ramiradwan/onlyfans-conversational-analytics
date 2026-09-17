import { Box, Stack } from '@mui/material';
import { StatusChip } from 'onlyfans-analytics-frontend';

export function Tones() {
  return (
    <Box sx={{ bgcolor: 'background.default', p: 2 }}>
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <StatusChip label="Up to date" tone="success" />
        <StatusChip label="Updating" tone="info" />
        <StatusChip label="Not connected" tone="default" />
        <StatusChip label="Connection interrupted" tone="warning" />
        <StatusChip label="Not available" tone="error" />
      </Stack>
    </Box>
  );
}
