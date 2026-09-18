import { ChartPlaceholder, Panel, Typography } from 'onlyfans-analytics-frontend';

import './card.module.css';

export function DashboardChart() {
  return (
    <Panel sx={{ maxWidth: 760 }}>
      <Typography variant="h6">Sentiment over time</Typography>
      <ChartPlaceholder height={260} />
    </Panel>
  );
}

export function CompactChart() {
  return (
    <Panel sx={{ maxWidth: 520, p: 2 }}>
      <ChartPlaceholder height={180} />
    </Panel>
  );
}
