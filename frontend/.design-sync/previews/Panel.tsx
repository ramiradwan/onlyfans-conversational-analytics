import {
  Panel,
  Stack,
  StatusChip,
  Typography,
} from 'onlyfans-analytics-frontend';

import './card.module.css';

export function InsightSummary() {
  return (
    <Panel sx={{ maxWidth: 560 }}>
      <Stack
        direction="row"
        sx={{
          justifyContent: 'space-between',
          alignItems: 'center'
        }}>
        <Typography variant="h6">Most discussed topic</Typography>
        <StatusChip label="Up to date" tone="success" />
      </Stack>
      <Typography variant="body1">
        Behind-the-scenes content came up in 18 of your last 50 conversations.
      </Typography>
      <Typography variant="body2" sx={{
        color: 'text.secondary'
      }}>
        Based on messages stored on this computer.
      </Typography>
    </Panel>
  );
}

export function Emphasis() {
  return (
    <Stack spacing={2} sx={{ maxWidth: 560 }}>
      <Panel emphasis="dominant">
        <Typography variant="subtitle2" sx={{ color: 'text.secondary' }}>Messages this week</Typography>
        <Typography variant="kpi">2,436</Typography>
      </Panel>
      <Panel>
        <Typography variant="subtitle2">Most discussed topic</Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Behind-the-scenes content came up in 18 of your last 50 conversations.
        </Typography>
      </Panel>
      <Panel emphasis="quiet">
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Based on messages stored on this computer.
        </Typography>
      </Panel>
    </Stack>
  );
}

export function CompactSurface() {
  return (
    <Panel sx={{ maxWidth: 420, p: 2, gap: 1 }}>
      <Typography variant="subtitle2">Waiting for replies</Typography>
      <Typography variant="body2" sx={{
        color: 'text.secondary'
      }}>
        3 conversations have a new message from a fan.
      </Typography>
    </Panel>
  );
}
