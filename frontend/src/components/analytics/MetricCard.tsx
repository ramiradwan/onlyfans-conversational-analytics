import { Box, Card, Stack, Typography, styled } from '@mui/material';
import type { ReactNode } from 'react';

import { AnalyticsWindowLabel } from './AnalyticsWindowLabel';
import type { AnalyticsWindowSource } from '../../analytics';
import { componentTokens } from '../../theme';

export type MetricTone = 'primary' | 'sentiment' | 'opportunity' | 'connection';

export interface MetricCardProps {
  label: string;
  value: string;
  supportingText: string;
  icon: ReactNode;
  tone?: MetricTone;
  labelComponent?: 'div' | 'h2' | 'h3' | 'p';
  windowSource: AnalyticsWindowSource;
}

const Root = styled(Card)(({ theme }) => ({
  height: '100%',
  padding: theme.spacing(2),
}));

const IconTile = styled(Box, {
  shouldForwardProp: (property) => property !== '$tone',
})<{ $tone: MetricTone }>(({ theme, $tone }) => {
  const color =
    $tone === 'sentiment'
      ? theme.vars.palette.chart.positive
      : $tone === 'opportunity'
        ? theme.vars.palette.chart.opportunity
        : $tone === 'connection'
          ? theme.vars.palette.secondary.main
          : theme.vars.palette.primary.main;
  return {
    alignItems: 'center',
    backgroundColor: `color-mix(in srgb, ${color} 12%, ${theme.vars.palette.background.paper})`,
    border: `1px solid color-mix(in srgb, ${color} 28%, ${theme.vars.palette.background.paper})`,
    borderRadius: Number(theme.shape.borderRadius) * 2,
    color,
    display: 'flex',
    flex: '0 0 auto',
    height: componentTokens.analytics.metricIconSize,
    justifyContent: 'center',
    width: componentTokens.analytics.metricIconSize,
  };
});

const Value = styled('p')(({ theme }) => ({
  ...theme.typography.h5,
  color: theme.vars.palette.text.primary,
  fontVariantNumeric: 'tabular-nums',
  letterSpacing: '-0.02em',
  margin: 0,
}));

export function MetricCard({
  label,
  value,
  supportingText,
  icon,
  tone = 'primary',
  labelComponent = 'p',
  windowSource,
}: MetricCardProps) {
  return (
    <Root role="group" aria-label={`${label} metric`}>
      <Stack direction="row" spacing={2} alignItems="flex-start">
        <IconTile $tone={tone} aria-hidden="true">
          {icon}
        </IconTile>
        <Box minWidth={0}>
          <Typography component={labelComponent} variant="body2" color="text.secondary">
            {label}
          </Typography>
          <Value>{value}</Value>
          <Typography component="p" variant="caption" color="text.secondary">
            {supportingText}
          </Typography>
          <AnalyticsWindowLabel source={windowSource} />
        </Box>
      </Stack>
    </Root>
  );
}

const Row = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(2),
  gridTemplateColumns: 'minmax(0, 1fr)',
  [theme.breakpoints.up('sm')]: {
    gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
  },
  [theme.breakpoints.up('lg')]: {
    gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
  },
}));

export interface MetricRowProps {
  items: readonly MetricCardProps[];
}

export function MetricRow({ items }: MetricRowProps) {
  return (
    <Row aria-label="Analytics key metrics">
      {items.map((item) => (
        <MetricCard key={item.label} {...item} />
      ))}
    </Row>
  );
}
