import { Chip, Stack, Typography } from '@mui/material';
import { Panel } from 'onlyfans-analytics-frontend';

export function InsightSummary() {
  return (
    <Panel sx={{ maxWidth: 560 }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center">
        <Typography variant="h6">Audience insight</Typography>
        <Chip label="Updated now" size="small" color="success" variant="outlined" />
      </Stack>
      <Typography variant="body1">
        Positive sentiment rose after the latest behind-the-scenes release.
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Responses mentioning custom content generated the strongest retention signal.
      </Typography>
    </Panel>
  );
}

export function CompactSurface() {
  return (
    <Panel sx={{ maxWidth: 420, p: 2, gap: 1 }}>
      <Typography variant="subtitle2">Next action</Typography>
      <Typography variant="body2" color="text.secondary">
        Follow up with three high-intent fans before 18:00.
      </Typography>
    </Panel>
  );
}
