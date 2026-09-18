import { Box, Typography, styled } from '@mui/material';

import {
  formatCount,
  formatDecimal,
  formatRatioPercentParts,
  formatMinutesParts,
  type AnalyticsResponseMetrics,
} from '../../analytics';
import { componentTokens, layoutTokens } from '../../theme';
import { barArrival } from '../../theme/presentationMotion';

const UNAVAILABLE = '—';

function NumberParts({ parts }: { parts: Intl.NumberFormatPart[] | null }) {
  if (parts === null) return <>{UNAVAILABLE}</>;
  return <>{parts.map((part, index) => (
    part.type === 'unit' || part.type === 'percentSign' || part.type === 'literal'
      ? <Typography component="span" variant="metricUnit" data-metric-unit key={index} sx={{ color: 'text.secondary' }}>{part.value}</Typography>
      : <span key={index}>{part.value}</span>
  ))}</>;
}

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
    ? null
    : formatMinutesParts(metrics.averageHandlingMinutes);
  const coveragePercent = metrics.responseCoverage === null
    ? null
    : formatRatioPercentParts(metrics.responseCoverage);
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
            <NumberParts parts={replyTime} />
          </MetricValue>
        </Metric>
        <Metric>
          <Typography component="dt" variant="body2" sx={{ color: 'text.secondary' }}>
            Messages you replied to
          </Typography>
          <MetricValue data-visual="reply-metric-value">
            <NumberParts parts={coveragePercent} />
          </MetricValue>
          {metrics.responseCoverage !== null && (
            <Box aria-hidden="true" data-visual="reply-coverage-track" sx={{
              bgcolor: 'surface.subtle', borderRadius: `${layoutTokens.radius.pill}px`,
              height: componentTokens.analytics.coverageHeight, overflow: 'hidden', mt: 1, mb: 0.5,
            }}>
              <Box data-visual="reply-coverage-fill" sx={{ bgcolor: 'measurement.main', height: '100%', borderRadius: 'inherit', ...barArrival }}
                style={{ width: `${Math.min(1, Math.max(0, metrics.responseCoverage)) * 100}%` }} />
            </Box>
          )}
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
