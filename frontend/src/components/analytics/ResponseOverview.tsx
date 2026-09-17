import { Box, Typography, styled } from '@mui/material';

import {
  formatCount,
  formatDecimal,
  formatRatioPercent,
  type AnalyticsResponseMetrics,
} from '../../analytics';

const UNAVAILABLE = '—';

const Metrics = styled('dl')(({ theme }) => ({
  display: 'grid',
  flex: 1,
  margin: 0,
  minWidth: 0,
  [theme.breakpoints.up('md')]: {
    gridTemplateRows: 'repeat(3, minmax(0, 1fr))',
  },
}));

const Metric = styled(Box)(({ theme }) => ({
  display: 'flex',
  flexDirection: 'column',
  justifyContent: 'center',
  margin: 0,
  minHeight: 72,
  paddingBlock: theme.spacing(1.5),
  '& + &': {
    borderTop: `1px solid ${theme.vars.palette.divider}`,
  },
}));

const MetricValue = styled('dd')(({ theme }) => ({
  ...theme.typography.metric,
  color: theme.vars.palette.text.primary,
  margin: 0,
  marginTop: theme.spacing(0.25),
}));

const MetricSupport = styled('span')(({ theme }) => ({
  ...theme.typography.caption,
  color: theme.vars.palette.text.secondary,
  marginTop: theme.spacing(0.25),
}));

export interface ResponseOverviewProps {
  metrics: AnalyticsResponseMetrics;
}

export function ResponseOverview({ metrics }: ResponseOverviewProps) {
  const replyTime = metrics.averageHandlingMinutes === null
    ? UNAVAILABLE
    : `${formatDecimal(metrics.averageHandlingMinutes)} min`;
  const coveragePercent = metrics.responseCoverage === null
    ? UNAVAILABLE
    : formatRatioPercent(metrics.responseCoverage);
  const coverageSupport = metrics.responseCoverage === null
    ? null
    : `${formatCount(metrics.respondedCount)} of ${formatCount(metrics.responseOpportunityCount)} messages`;
  const turns = metrics.turns === null ? UNAVAILABLE : formatDecimal(metrics.turns, 0);

  return (
    <Box data-visual="reply-metrics" sx={{ display: 'flex', flex: 1, minHeight: 0 }}>
      <Metrics>
        <Metric>
          <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>
            Average reply time
          </Typography>
          <MetricValue data-visual="reply-metric-value">
            {replyTime}
          </MetricValue>
        </Metric>
        <Metric>
          <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>
            Messages you replied to
          </Typography>
          <MetricValue data-visual="reply-metric-value">
            {coveragePercent}
          </MetricValue>
          {coverageSupport && <MetricSupport>{coverageSupport}</MetricSupport>}
        </Metric>
        <Metric>
          <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>
            Turns per conversation
          </Typography>
          <MetricValue data-visual="reply-metric-value">
            {turns}
          </MetricValue>
        </Metric>
      </Metrics>
    </Box>
  );
}
