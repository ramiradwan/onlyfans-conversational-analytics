import { Box, Stack, Typography, styled, useTheme } from '@mui/material';
import { useId, useMemo, useState } from 'react';

import {
  formatDateLabel,
  formatRatioPercent,
  formatSentimentScore,
  sentimentLabel,
  type AnalyticsTrendPoint,
} from '../../analytics';
import { componentTokens } from '../../theme';

const WIDTH = 640;
const HEIGHT = 260;
const LEFT = 52;
const RIGHT = 28;
const TOP = 18;
const BOTTOM = 38;
const PLOT_WIDTH = WIDTH - LEFT - RIGHT;
const PLOT_HEIGHT = HEIGHT - TOP - BOTTOM;
const ZERO_Y = TOP + PLOT_HEIGHT / 2;

type Polarity = 'positive' | 'neutral' | 'negative' | 'engagement';

interface ActivePoint {
  series: 'sentiment' | 'engagement';
  index: number;
  left: number;
  top: number;
  label: string;
}

const Plot = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  borderRadius: theme.shape.borderRadius,
  height: componentTokens.analytics.chartHeight,
  minHeight: componentTokens.analytics.chartHeight,
  overflow: 'hidden',
  position: 'relative',
  ...theme.effects.chartFrame(theme),
}));

const ChartSvg = styled('svg')({
  display: 'block',
  height: '100%',
  inset: 0,
  overflow: 'visible',
  position: 'absolute',
  width: '100%',
});

const AxisLabel = styled('span')(({ theme }) => ({
  color: theme.vars.palette.text.secondary,
  fontSize: theme.typography.pxToRem(12),
  lineHeight: 1,
  pointerEvents: 'none',
  position: 'absolute',
  transform: 'translate(-100%, -50%)',
}));

const MarkButton = styled('button', {
  shouldForwardProp: (property) => property !== '$polarity',
})<{ $polarity: Polarity }>(({ theme, $polarity }) => {
  const color =
    $polarity === 'positive'
      ? theme.vars.palette.chart.positive
      : $polarity === 'negative'
        ? theme.vars.palette.chart.negative
        : $polarity === 'engagement'
          ? theme.vars.palette.chart.categorical2
          : theme.vars.palette.chart.neutral;
  return {
    alignItems: 'center',
    appearance: 'none',
    background: 'transparent',
    border: 0,
    color,
    cursor: 'help',
    display: 'flex',
    height: componentTokens.analytics.markHitTarget,
    justifyContent: 'center',
    margin: 0,
    padding: 0,
    position: 'absolute',
    transform: 'translate(-50%, -50%)',
    width: componentTokens.analytics.markHitTarget,
    zIndex: 2,
    '&::before': {
      backgroundColor: theme.vars.palette.background.paper,
      border: `2px solid ${color}`,
      borderRadius: $polarity === 'neutral' ? Number(theme.shape.borderRadius) / 2 : '50%',
      content: '""',
      height: componentTokens.analytics.markSize,
      position: 'absolute',
      width: componentTokens.analytics.markSize,
    },
    '&:focus-visible': {
      borderRadius: '50%',
      outline: `2px solid ${theme.vars.palette.primary.main}`,
      outlineOffset: 0,
    },
  };
});

const MarkerSymbol = styled('span')(({ theme }) => ({
  color: 'currentColor',
  fontSize: theme.typography.pxToRem(10),
  fontWeight: theme.typography.fontWeightBold,
  lineHeight: 1,
  position: 'relative',
  transform: 'translateY(-1px)',
  zIndex: 1,
}));

const TooltipBox = styled(Box)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  border: `1px solid ${theme.vars.palette.divider}`,
  borderRadius: theme.shape.borderRadius,
  boxShadow: theme.vars.palette.surface.elevation,
  maxWidth: theme.spacing(28),
  padding: theme.spacing(1, 1.5),
  pointerEvents: 'none',
  position: 'absolute',
  transform: 'translate(-50%, calc(-100% - 8px))',
  zIndex: theme.zIndex.tooltip,
}));

const Legend = styled(Stack)(({ theme }) => ({
  alignItems: 'center',
  flexDirection: 'row',
  flexWrap: 'wrap',
  gap: theme.spacing(2),
}));

const LegendItem = styled(Stack)(({ theme }) => ({
  alignItems: 'center',
  flexDirection: 'row',
  gap: theme.spacing(0.75),
}));

const LegendLine = styled('span', {
  shouldForwardProp: (property) => property !== '$color',
})<{ $color: string }>(({ $color }) => ({
  backgroundColor: $color,
  borderRadius: componentTokens.analytics.dataEndRadius,
  display: 'inline-block',
  height: 2,
  width: 24,
}));

const DateExtent = styled(Stack)(({ theme }) => ({
  flexDirection: 'row',
  justifyContent: 'space-between',
  paddingInline: theme.spacing(1),
}));

const Details = styled('details')(({ theme }) => ({
  '& > summary': {
    borderRadius: theme.shape.borderRadius,
    color: theme.vars.palette.primary.main,
    cursor: 'pointer',
    fontSize: theme.typography.body2.fontSize,
    fontWeight: theme.typography.fontWeightMedium,
    padding: theme.spacing(0.75),
    width: 'fit-content',
  },
  '& > summary:focus-visible': {
    outline: `2px solid ${theme.vars.palette.primary.main}`,
    outlineOffset: 2,
  },
}));

const TableScroller = styled(Box)(({ theme }) => ({
  marginTop: theme.spacing(1),
  overflowX: 'auto',
  '& table': {
    borderCollapse: 'collapse',
    fontSize: theme.typography.body2.fontSize,
    width: '100%',
  },
  '& th, & td': {
    borderBottom: `1px solid ${theme.vars.palette.divider}`,
    padding: theme.spacing(1),
    textAlign: 'start',
  },
  '& th': {
    color: theme.vars.palette.text.secondary,
    fontWeight: theme.typography.fontWeightMedium,
  },
  '& td:not(:first-of-type)': {
    fontVariantNumeric: 'tabular-nums',
  },
}));

function xFor(index: number, count: number): number {
  if (count <= 1) return LEFT + PLOT_WIDTH / 2;
  return LEFT + (index / (count - 1)) * PLOT_WIDTH;
}

function yForSentiment(value: number): number {
  return TOP + ((1 - value) / 2) * PLOT_HEIGHT;
}

function yForRatio(value: number): number {
  return TOP + (1 - value) * PLOT_HEIGHT;
}

function linePath(points: readonly AnalyticsTrendPoint[], ratio = false): string {
  return points
    .map((point, index) => {
      const x = xFor(index, points.length);
      const y = ratio ? yForRatio(point.value) : yForSentiment(point.value);
      return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(' ');
}

function areaPath(points: readonly AnalyticsTrendPoint[]): string {
  if (points.length === 0) return '';
  const firstX = xFor(0, points.length);
  const lastX = xFor(points.length - 1, points.length);
  return `${linePath(points)} L ${lastX.toFixed(2)} ${ZERO_Y.toFixed(2)} L ${firstX.toFixed(2)} ${ZERO_Y.toFixed(2)} Z`;
}

function pointPolarity(value: number): Polarity {
  if (value > 0.05) return 'positive';
  if (value < -0.05) return 'negative';
  return 'neutral';
}

function markerSymbol(polarity: Polarity): string {
  if (polarity === 'positive') return '+';
  if (polarity === 'negative') return '−';
  if (polarity === 'neutral') return '•';
  return '×';
}

/** A translucent wash of `color` suitable for an SVG gradient stop, via color-mix. */
function wash(color: string): string {
  return `color-mix(in srgb, ${color} 10%, transparent)`;
}

export interface SentimentEngagementTrendProps {
  sentiment: readonly AnalyticsTrendPoint[];
  engagement?: readonly AnalyticsTrendPoint[];
}

export function SentimentEngagementTrend({
  sentiment,
  engagement,
}: SentimentEngagementTrendProps) {
  const theme = useTheme();
  const gradientId = useId().replace(/:/g, '');
  const washId = useId().replace(/:/g, '');
  const [active, setActive] = useState<ActivePoint | null>(null);
  const engagementByDate = useMemo(
    () => new Map((engagement ?? []).map((point) => [point.at, point])),
    [engagement],
  );

  if (sentiment.length === 0) {
    return (
      <Typography sx={{
        color: 'text.secondary'
      }}>
        No sentiment observations are available for the stated data window.
      </Typography>
    );
  }

  const latestSentiment = sentiment[sentiment.length - 1];
  const latestEngagement = engagement?.[engagement.length - 1];
  const tooltipId = gradientId + '-tooltip';

  return (
    <Stack spacing={1.5}>
      <Legend aria-label={engagement?.length ? 'Chart legend' : 'Series label'}>
        <LegendItem>
          <LegendLine $color={theme.vars.palette.chart.positive} aria-hidden="true" />
          <Typography variant="caption">
            Sentiment {formatSentimentScore(latestSentiment.value)} (−1 to +1)
          </Typography>
        </LegendItem>
        {latestEngagement && (
          <LegendItem>
            <LegendLine $color={theme.vars.palette.chart.categorical2} aria-hidden="true" />
            <Typography variant="caption">
              Engagement {formatRatioPercent(latestEngagement.value)}
            </Typography>
          </LegendItem>
        )}
      </Legend>

      <Plot>
        <ChartSvg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          preserveAspectRatio="none"
          role="img"
          aria-label="Sentiment trend from negative one to positive one"
        >
          <defs>
            <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={theme.vars.palette.chart.positive} />
              <stop offset="50%" stopColor={theme.vars.palette.chart.neutral} />
              <stop offset="100%" stopColor={theme.vars.palette.chart.negative} />
            </linearGradient>
            <linearGradient id={washId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={wash(theme.vars.palette.chart.positive)} />
              <stop offset="50%" stopColor={wash(theme.vars.palette.chart.neutral)} />
              <stop offset="100%" stopColor={wash(theme.vars.palette.chart.negative)} />
            </linearGradient>
          </defs>
          {[1, 0, -1].map((tick) => {
            const y = yForSentiment(tick);
            return (
              <g key={tick}>
                <line
                  x1={LEFT}
                  x2={WIDTH - RIGHT}
                  y1={y}
                  y2={y}
                  stroke={theme.vars.palette.chart.grid}
                  strokeWidth="1"
                  vectorEffect="non-scaling-stroke"
                />
              </g>
            );
          })}
          <line
            x1={LEFT}
            x2={LEFT}
            y1={TOP}
            y2={HEIGHT - BOTTOM}
            stroke={theme.vars.palette.chart.grid}
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
          <path d={areaPath(sentiment)} fill={`url(#${washId})`} />
          <path
            d={linePath(sentiment)}
            fill="none"
            stroke={`url(#${gradientId})`}
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
          {engagement?.length && (
            <path
              d={linePath(engagement, true)}
              fill="none"
              stroke={theme.vars.palette.chart.categorical2}
              strokeDasharray="6 4"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth="2"
              vectorEffect="non-scaling-stroke"
            />
          )}
        </ChartSvg>

        {[1, 0, -1].map((tick) => (
          <AxisLabel
            key={'axis-' + tick}
            aria-hidden="true"
            style={{
              left: `${((LEFT - 10) / WIDTH) * 100}%`,
              top: `${(yForSentiment(tick) / HEIGHT) * 100}%`,
            }}
          >
            {tick > 0 ? '+1' : String(tick)}
          </AxisLabel>
        ))}

        {sentiment.map((point, index) => {
          const x = xFor(index, sentiment.length);
          const y = yForSentiment(point.value);
          const polarity = pointPolarity(point.value);
          const label = `${formatDateLabel(point.at)}: ${sentimentLabel(point.value)} sentiment, ${formatSentimentScore(point.value)}, ${point.sampleCount} messages`;
          return (
            <MarkButton
              key={'sentiment-' + point.at}
              type="button"
              $polarity={polarity}
              aria-label={label}
              aria-describedby={active?.series === 'sentiment' && active.index === index ? tooltipId : undefined}
              data-hit-target={componentTokens.analytics.markHitTarget}
              style={{ left: `${(x / WIDTH) * 100}%`, top: `${(y / HEIGHT) * 100}%` }}
              onMouseEnter={() => setActive({ series: 'sentiment', index, left: x, top: y, label })}
              onMouseLeave={() => setActive(null)}
              onFocus={() => setActive({ series: 'sentiment', index, left: x, top: y, label })}
              onBlur={() => setActive(null)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') setActive(null);
              }}
            >
              <MarkerSymbol aria-hidden="true">{markerSymbol(polarity)}</MarkerSymbol>
            </MarkButton>
          );
        })}

        {engagement?.map((point, index) => {
          const x = xFor(index, engagement.length);
          const y = yForRatio(point.value);
          const label = `${formatDateLabel(point.at)}: engagement ${formatRatioPercent(point.value)}, ${point.sampleCount} observations`;
          return (
            <MarkButton
              key={'engagement-' + point.at}
              type="button"
              $polarity="engagement"
              aria-label={label}
              aria-describedby={active?.series === 'engagement' && active.index === index ? tooltipId : undefined}
              data-hit-target={componentTokens.analytics.markHitTarget}
              style={{ left: `${(x / WIDTH) * 100}%`, top: `${(y / HEIGHT) * 100}%` }}
              onMouseEnter={() => setActive({ series: 'engagement', index, left: x, top: y, label })}
              onMouseLeave={() => setActive(null)}
              onFocus={() => setActive({ series: 'engagement', index, left: x, top: y, label })}
              onBlur={() => setActive(null)}
            >
              <MarkerSymbol aria-hidden="true">{markerSymbol('engagement')}</MarkerSymbol>
            </MarkButton>
          );
        })}

        {active && (
          <TooltipBox
            id={tooltipId}
            role="tooltip"
            style={{
              left: `${Math.min(90, Math.max(10, (active.left / WIDTH) * 100))}%`,
              top: `${Math.max(18, (active.top / HEIGHT) * 100)}%`,
            }}
          >
            <Typography variant="caption">{active.label}</Typography>
          </TooltipBox>
        )}
      </Plot>

      <DateExtent aria-hidden="true">
        <Typography variant="caption" sx={{
          color: 'text.secondary'
        }}>
          {formatDateLabel(sentiment[0].at)}
        </Typography>
        <Typography variant="caption" sx={{
          color: 'text.secondary'
        }}>
          {formatDateLabel(latestSentiment.at)}
        </Typography>
      </DateExtent>

      {!engagement?.length && (
        <Typography variant="caption" sx={{
          color: 'text.secondary'
        }}>
          Engagement trend is unavailable from the current bounded projection.
        </Typography>
      )}

      <Details>
        <summary>View data table</summary>
        <TableScroller>
          <table aria-label="Sentiment and engagement trend data">
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Sentiment (−1 to +1)</th>
                <th scope="col">Messages</th>
                {engagement?.length ? <th scope="col">Engagement</th> : null}
              </tr>
            </thead>
            <tbody>
              {sentiment.map((point) => (
                <tr key={point.at}>
                  <td>{formatDateLabel(point.at)}</td>
                  <td>
                    {sentimentLabel(point.value)} · {formatSentimentScore(point.value)}
                  </td>
                  <td>{point.sampleCount}</td>
                  {engagement?.length ? (
                    <td>
                      {engagementByDate.has(point.at)
                        ? formatRatioPercent(engagementByDate.get(point.at)?.value ?? 0)
                        : 'Unavailable'}
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroller>
      </Details>
    </Stack>
  );
}
