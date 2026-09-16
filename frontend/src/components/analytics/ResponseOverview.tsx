import { Box, Typography, styled } from '@mui/material';

import {
  formatCount,
  formatDecimal,
  formatRatioPercent,
  type AnalyticsResponseMetrics,
} from '../../analytics';

const UNAVAILABLE = '—';

const List = styled('dl')(({ theme }) => ({
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 1fr) auto',
  margin: 0,
  '& dt, & dd': {
    borderBottom: `1px solid ${theme.vars.palette.divider}`,
    margin: 0,
    padding: theme.spacing(1.25, 0),
  },
  '& dt': {
    color: theme.vars.palette.text.secondary,
  },
  '& dd': {
    fontVariantNumeric: 'tabular-nums',
    fontWeight: theme.typography.fontWeightMedium,
    paddingInlineStart: theme.spacing(2),
    textAlign: 'end',
  },
}));

export interface ResponseOverviewProps {
  metrics: AnalyticsResponseMetrics;
}

export function ResponseOverview({ metrics }: ResponseOverviewProps) {
  const replyTime = metrics.averageHandlingMinutes === null
    ? UNAVAILABLE
    : `${formatDecimal(metrics.averageHandlingMinutes)} min`;
  const repliedTo = metrics.responseCoverage === null
    ? UNAVAILABLE
    : `${formatRatioPercent(metrics.responseCoverage)} (${formatCount(metrics.respondedCount)} of ${formatCount(metrics.responseOpportunityCount)})`;
  const turns = metrics.turns === null ? UNAVAILABLE : formatDecimal(metrics.turns, 0);
  return (
    <Box>
      <List>
        <Typography component="dt" variant="body2">
          Average reply time
        </Typography>
        <Typography component="dd" variant="body2">
          {replyTime}
        </Typography>
        <Typography component="dt" variant="body2">
          Messages you replied to
        </Typography>
        <Typography component="dd" variant="body2">
          {repliedTo}
        </Typography>
        <Typography component="dt" variant="body2">
          Turns per conversation
        </Typography>
        <Typography component="dd" variant="body2">
          {turns}
        </Typography>
      </List>
    </Box>
  );
}
