import { Box, Stack, Typography, styled } from '@mui/material';
import { useMemo } from 'react';

import {
  formatCount,
  formatPercentValue,
  type AnalyticsTopicMetric,
} from '../../analytics';
import { componentTokens } from '../../theme';

const DesktopScroller = styled(Box)(({ theme }) => ({
  display: 'none',
  overflowX: 'auto',
  [theme.breakpoints.up('sm')]: { display: 'block' },
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

const NarrowTopics = styled('ul')(({ theme }) => ({
  listStyle: 'none',
  margin: 0,
  padding: 0,
  [theme.breakpoints.up('sm')]: { display: 'none' },
}));

const NarrowTopic = styled('li')(({ theme }) => ({
  borderBottom: `1px solid ${theme.vars.palette.divider}`,
  display: 'grid',
  gap: theme.spacing(0.75),
  padding: theme.spacing(1.75, 0),
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
  backgroundColor: theme.vars.palette.chart.sentiment,
  borderRadius: `0 ${componentTokens.analytics.dataEndRadius}px ${componentTokens.analytics.dataEndRadius}px 0`,
  height: '100%',
  minWidth: componentTokens.analytics.barGap,
}));

const NarrowMetadata = styled(Stack)(({ theme }) => ({
  color: theme.vars.palette.text.secondary,
  flexDirection: 'row',
  justifyContent: 'space-between',
  gap: theme.spacing(2),
  '& .MuiTypography-root': {
    fontVariantNumeric: 'tabular-nums',
  },
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
    return <Typography sx={{ color: 'text.secondary' }}>No topics found for these dates.</Typography>;
  }

  return (
    <>
      <NarrowTopics aria-label="Topics and trend">
        {topics.map((topic) => (
          <NarrowTopic key={topic.id}>
            <Typography component="p" variant="subtitle2">{topic.label}</Typography>
            <MagnitudeCell>
              <BarTrack aria-hidden="true">
                <Bar style={{ width: `${(topic.volume / maximum) * 100}%` }} />
              </BarTrack>
              <Typography component="span" variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {formatCount(topic.volume)} messages
              </Typography>
            </MagnitudeCell>
            <NarrowMetadata>
              <Typography variant="caption">Share {formatPercentValue(topic.sharePercent)}</Typography>
              <Typography variant="caption">
                Change {topic.trendPercent === null ? '—' : formatPercentValue(topic.trendPercent)}
              </Typography>
            </NarrowMetadata>
          </NarrowTopic>
        ))}
      </NarrowTopics>

      <DesktopScroller>
        <table aria-label="Topics and trend">
          <thead>
            <tr>
              <th scope="col">Topic</th>
              <th scope="col">Messages</th>
              <th scope="col">Share</th>
              <th scope="col">Change from previous period</th>
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
                <td>{topic.trendPercent === null ? '—' : formatPercentValue(topic.trendPercent)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </DesktopScroller>
    </>
  );
}
