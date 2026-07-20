import { Box, Typography, styled } from '@mui/material';

import {
  formatCount,
  formatDecimal,
  formatPercentValue,
  formatRatioPercent,
  type AnalyticsResponseMetrics,
} from '../../analytics';

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
  return (
    <Box>
      <List>
        <Typography component="dt" variant="body2">
          Average handling time
        </Typography>
        <Typography component="dd" variant="body2">
          {formatDecimal(metrics.averageHandlingMinutes)} min
        </Typography>
        <Typography component="dt" variant="body2">
          Reply coverage
        </Typography>
        <Typography component="dd" variant="body2">
          {formatRatioPercent(metrics.responseCoverage)}
        </Typography>
        <Typography component="dt" variant="body2">
          Silence rate
        </Typography>
        <Typography component="dd" variant="body2">
          {formatPercentValue(metrics.silencePercent)}
        </Typography>
        <Typography component="dt" variant="body2">
          Responded opportunities
        </Typography>
        <Typography component="dd" variant="body2">
          {formatCount(metrics.respondedCount)} / {formatCount(metrics.responseOpportunityCount)}
        </Typography>
        <Typography component="dt" variant="body2">
          Conversation turns
        </Typography>
        <Typography component="dd" variant="body2">
          {formatDecimal(metrics.turns, 0)}
        </Typography>
      </List>
    </Box>
  );
}
