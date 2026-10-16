import { Box, Stack, Typography, styled } from '@mui/material';
import { useMemo, type CSSProperties } from 'react';

import {
  formatCount,
  formatPercentValue,
  type AnalyticsTopicMetric,
} from '../../analytics';
import { componentTokens, layoutTokens } from '../../theme';
import { barArrival } from '../../theme/presentationMotion';
import { ReservedRegion } from '../ui/ReservedRegion';

// The table shows once its panel holds topicTableMinWidth, and the list shows below that.
const tableFits = `@container topics (min-width: ${componentTokens.analytics.topicTableMinWidth})`;

/**
 * Heights of the topics region. Padding stays fixed while text scales, so each size counts both and cuts a row at the
 * same place at any text size. A listed topic is 41px and 3.75rem, a table row 17px and 1.3125rem, and the table header
 * 17px and 2.0625rem.
 */
export const topicRegionSize = {
  list: 'calc(4.35 * (41px + 3.75rem))',
  table: 'calc(17px + 2.0625rem + 7.5 * (17px + 1.3125rem))',
};

const WideTopics = styled(Box)(({ theme }) => ({
  display: 'none',
  [tableFits]: { display: 'block' },
  '& table': {
    display: 'grid',
    gridTemplateColumns: componentTokens.analytics.topicColumns,
    fontSize: theme.typography.body2.fontSize,
    width: '100%',
  },
  '& thead, & tbody, & tr': { display: 'grid', gridColumn: '1 / -1', gridTemplateColumns: 'subgrid' },
  '& th, & td': {
    minWidth: 0,
    alignContent: 'center',
    borderBottom: `1px solid ${theme.vars.palette.divider}`,
    padding: theme.spacing(1, 1.25),
    textAlign: 'start',
    verticalAlign: 'middle',
  },
  '& th': {
    ...theme.typography.tableHeading,
    color: theme.vars.palette.text.muted,
  },
  '& td:not(:first-of-type)': {
    ...theme.typography.numericBody,
  },
}));

const NarrowTopics = styled('ul')({
  listStyle: 'none',
  margin: 0,
  padding: 0,
  [tableFits]: { display: 'none' },
});

const NarrowTopic = styled('li')(({ theme }) => ({
  borderBottom: `1px solid ${theme.vars.palette.divider}`,
  display: 'grid',
  gap: theme.spacing(0.75),
  padding: theme.spacing(1.75, 0),
}));

const MagnitudeCell = styled(Box, { shouldForwardProp: (p) => p !== '$labelled' })<{ $labelled?: boolean }>(({ theme, $labelled }) => ({
  alignItems: 'center',
  display: 'grid',
  gap: theme.spacing(1),
  minWidth: 0,
  gridTemplateColumns: $labelled ? 'minmax(0, 1fr) minmax(0, calc(var(--topic-count-width) + 9ch))' : 'minmax(0, 1fr) var(--topic-count-width)',
}));

const BarTrack = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.surface.subtle,
  height: componentTokens.analytics.barThickness,
  minWidth: 0,
  borderRadius: `${layoutTokens.radius.pill}px`,
  overflow: 'hidden',
}));

const Bar = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.measurement.main,
  borderRadius: 'inherit',
  height: '100%',
  ...barArrival,
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

  const countWidth = `${Math.max(3, ...topics.map((topic) => formatCount(topic.volume).length))}ch`;
  const columnStyle = { '--topic-count-width': countWidth } as CSSProperties;

  if (topics.length === 0) {
    return <Typography sx={{ color: 'text.secondary' }}>No topics found for these dates.</Typography>;
  }

  // A scrolling list shows part of its next row.
  return (
    <Box sx={{ containerName: 'topics', containerType: 'inline-size' }}>
      <ReservedRegion id="topics-list" regionRole="scroll" size={{ xs: topicRegionSize.list }} sx={{ [tableFits]: { blockSize: topicRegionSize.table } }}>
        <NarrowTopics data-region-content aria-label="Topics and trend" style={columnStyle}>
          {topics.map((topic) => (
            <NarrowTopic key={topic.id}>
              <Typography component="p" variant="subtitle2">{topic.label}</Typography>
              <MagnitudeCell $labelled>
                <BarTrack aria-hidden="true" data-visual="topic-track">
                  <Bar style={{ width: `${(topic.volume / maximum) * 100}%` }} />
                </BarTrack>
                <Typography component="span" variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                  <Typography component="span" variant="numericBody">{formatCount(topic.volume)}</Typography> messages
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

        <WideTopics data-region-content style={columnStyle}>
          <table aria-label="Topics and trend" role="table">
            <thead>
              <tr role="row">
                <th scope="col">Topic</th>
                <th scope="col">Messages</th>
                <th scope="col">Share</th>
                <th scope="col">Change from previous period</th>
              </tr>
            </thead>
            <tbody>
              {topics.map((topic) => (
                <tr role="row" key={topic.id}>
                  <td>{topic.label}</td>
                  <td>
                    <MagnitudeCell>
                      <BarTrack aria-hidden="true" data-visual="topic-track">
                        <Bar style={{ width: `${(topic.volume / maximum) * 100}%` }} />
                      </BarTrack>
                      <Typography component="span" variant="numericBody">{formatCount(topic.volume)}</Typography>
                    </MagnitudeCell>
                  </td>
                  <td>{formatPercentValue(topic.sharePercent)}</td>
                  <td>{topic.trendPercent === null ? '—' : formatPercentValue(topic.trendPercent)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </WideTopics>
      </ReservedRegion>
    </Box>
  );
}
