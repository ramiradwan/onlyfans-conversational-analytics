import { Box, Typography, styled } from '@mui/material';
import { useMemo } from 'react';

import {
  formatCount,
  formatPercentValue,
  type AnalyticsTopicMetric,
} from '../../analytics';
import { componentTokens } from '../../theme';

const Scroller = styled(Box)(({ theme }) => ({
  overflowX: 'auto',
  '& table': {
    borderCollapse: 'separate',
    borderSpacing: `0 ${componentTokens.analytics.barGap}px`,
    fontSize: theme.typography.body2.fontSize,
    minWidth: theme.spacing(70),
    width: '100%',
  },
  '& th, & td': {
    borderBottom: `1px solid ${theme.vars.palette.divider}`,
    padding: theme.spacing(1, 1.25),
    textAlign: 'start',
    verticalAlign: 'middle',
  },
  '& th': {
    color: theme.vars.palette.text.secondary,
    fontWeight: theme.typography.fontWeightMedium,
  },
  '& td:not(:first-of-type)': {
    fontVariantNumeric: 'tabular-nums',
  },
}));

const MagnitudeCell = styled(Box)(({ theme }) => ({
  alignItems: 'center',
  display: 'grid',
  gap: theme.spacing(1),
  gridTemplateColumns: `minmax(${theme.spacing(9)}, 1fr) auto`,
}));

const BarTrack = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.surface.subtle,
  height: componentTokens.analytics.barThickness,
  minWidth: theme.spacing(10),
  overflow: 'hidden',
}));

const Bar = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.chart.categorical1,
  borderRadius: `0 ${componentTokens.analytics.dataEndRadius}px ${componentTokens.analytics.dataEndRadius}px 0`,
  height: '100%',
  minWidth: componentTokens.analytics.barGap,
}));

export interface TopicsTableProps {
  topics: readonly AnalyticsTopicMetric[];
}

export function TopicsTable({ topics }: TopicsTableProps) {
  const maximum = useMemo(
    () => Math.max(1, ...topics.map((topic) => topic.volume)),
    [topics],
  );

  if (topics.length === 0) {
    return (
      <Typography sx={{
        color: 'text.secondary'
      }}>
        No topic observations are available for the stated data window.
      </Typography>
    );
  }

  return (
    <Scroller>
      <table aria-label="Topic magnitude and trend">
        <thead>
          <tr>
            <th scope="col">Topic</th>
            <th scope="col">Message volume</th>
            <th scope="col">Share</th>
            <th scope="col">Change vs. prior window</th>
          </tr>
        </thead>
        <tbody>
          {topics.map((topic) => (
            <tr key={topic.id}>
              <td>{topic.label}</td>
              <td>
                <MagnitudeCell>
                  <BarTrack aria-hidden="true">
                    <Bar style={{ width: `${(topic.volume / maximum) * 100}%` }} />
                  </BarTrack>
                  <span>{formatCount(topic.volume)}</span>
                </MagnitudeCell>
              </td>
              <td>{formatPercentValue(topic.sharePercent)}</td>
              <td>
                {topic.trendPercent === null
                  ? 'Unavailable'
                  : formatPercentValue(topic.trendPercent)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Scroller>
  );
}
