import { keyframes } from '@emotion/react';
import { Box, LinearProgress, Skeleton, Stack, Typography } from '@mui/material';
import { Children, useId, useState } from 'react';

import { surfaceArrival } from '../../theme/presentationMotion';
import { Panel } from '../ui/Panel';

const indeterminateBar = keyframes({
  '0%': { transform: 'translateX(-100%) scaleX(0.35)' },
  '50%': { transform: 'translateX(35%) scaleX(0.5)' },
  '100%': { transform: 'translateX(100%) scaleX(0.35)' },
});

export interface OverviewProgress {
  complete?: boolean;
  label: string;
  /** Percent complete, or null when the total is not known yet. */
  percent: number | null;
}
export interface DashboardOverviewProps {
  conversations: string;
  isLoading?: boolean;
  messages: string;
  progress?: OverviewProgress | null;
  received: string;
  sent: string;
  /** Raw counts for the received/sent bar; omitted when either is unknown. */
  split?: { received: number; sent: number } | null;
}

function CountValue({ value, step }: { value?: string; step: number }) {
  const [arrived, setArrived] = useState(false);
  const hasNumber = value !== undefined && /^\d/u.test(value);
  if (!arrived && hasNumber) setArrived(true);
  return <Box component="span" data-region-content sx={arrived ? surfaceArrival(step, true) : undefined}>
    {value ?? <Skeleton component="span" animation={false} width="100%"
      sx={{ display: 'inline-block', verticalAlign: 'baseline', height: '.72em' }} />}
  </Box>;
}

function Stat({ children, label, divider = false }: {
  children: React.ReactNode;
  label: string;
  divider?: boolean;
}) {
  const labelId = useId();
  const [value, ...support] = Children.toArray(children);
  return (
    <Box sx={(theme) => ({
      minWidth: 0, display: 'grid', alignItems: 'baseline', rowGap: 1,
      [theme.breakpoints.up('md')]: {
        gridTemplateRows: 'subgrid', gridRow: 'span 3',
        ...(divider ? { borderInlineStart: `1px solid ${theme.vars.palette.divider}`, pl: 5 } : {}),
      },
    })}>
      <Box role="group" aria-labelledby={labelId} sx={{
        display: 'grid', alignItems: 'baseline', rowGap: 1,
        gridTemplateRows: { md: 'subgrid' }, gridRow: { md: 'span 2' }, minWidth: 0,
      }}>
        <Typography id={labelId} variant="body2" sx={{ color: 'text.secondary' }}>
          {label}
        </Typography>
        {value}
      </Box>
      {support}
    </Box>
  );
}

function SplitLegend({ color, label, value }: { color: string; label: string; value: string | undefined }) {
  return (
    <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', minWidth: 0 }}>
      <Box aria-hidden="true" sx={{
        alignSelf: 'center', bgcolor: color, borderRadius: '50%', flexShrink: 0, height: 8, width: 8,
      }} />
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{label}</Typography>
      <Typography data-reserved-region={`metric-${label}`} variant="body2" sx={{ display: 'inline-block', width: '9ch', maxWidth: '100%', fontVariantNumeric: 'tabular-nums', fontWeight: 600, '& > span': { display: 'block' } }}>
        <CountValue value={value} step={2} />
      </Typography>
    </Stack>
  );
}

const RECEIVED_COLOR = 'measurement.main';
const SENT_COLOR = 'communication.outgoingBorder';
const headlineCount = { width: 'min(9ch, 100%)', lineHeight: 1.4, '& > span': { display: 'block' } } as const;

/** Headline counts share a baseline; narrow layouts keep the same reading order. */
export function DashboardOverview({
  conversations, isLoading = false, messages, progress = null, received, sent, split = null,
}: DashboardOverviewProps) {
  const total = split ? split.received + split.sent : 0;
  const receivedShare = total > 0 && split ? (split.received / total) * 100 : null;
  return (
    <Panel
      emphasis="dominant"
      component="section"
      aria-busy={isLoading}
      aria-label="Overview"
      data-journey-state="desktop.numbers_ready"
      sx={{
        display: 'grid', columnGap: { xs: 3, md: 5 }, rowGap: { xs: 3, md: 1 },
        gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'minmax(0, 2fr) minmax(0, 3fr)' },
        gridTemplateRows: { md: 'auto auto auto' }, p: { xs: 2.5, md: 3.5 }, animation: 'none',
      }}
    >
      <Stat label="Conversations">
        <Typography variant="kpi" data-reserved-region="metric-conversations" sx={headlineCount} data-visual="conversation-total"><CountValue value={isLoading ? undefined : conversations} step={0} /></Typography>
        {progress && (
          <Stack spacing={1} sx={{ maxWidth: 280, alignSelf: 'start' }}>
            <LinearProgress
              data-journey-state="desktop.history_syncing"
              sx={{ visibility: progress.complete ? 'hidden' : 'visible',
                ...(progress.percent === null ? {
                  '& .MuiLinearProgress-bar1': {
                    left: 0, right: 0, width: '100%', transformOrigin: 'left center',
                    animation: `${indeterminateBar} 1.8s ease-in-out infinite`,
                  },
                  '& .MuiLinearProgress-bar2': { display: 'none' },
                  '@media (prefers-reduced-motion: reduce)': { '& .MuiLinearProgress-bar': { display: 'none', animation: 'none' } },
                } : {
                  '& .MuiLinearProgress-bar': { animation: 'none', transform: 'none !important', clipPath: `inset(0 ${100 - progress.percent}% 0 0)` },
                  '& .MuiLinearProgress-bar2': { display: 'none' },
                }) }}
              aria-label={progress.label}
              variant={progress.percent === null ? 'indeterminate' : 'determinate'}
              value={progress.percent ?? undefined}
            />
            <Typography variant="caption" sx={{ color: 'text.secondary', minBlockSize: '2lh' }}>{progress.label}</Typography>
          </Stack>
        )}
      </Stat>
      <Stat label="Messages" divider>
        <Typography variant="metric" data-reserved-region="metric-messages" sx={headlineCount} data-visual="message-total"><CountValue value={isLoading ? undefined : messages} step={1} /></Typography>
        <Box sx={{ minWidth: 0, mt: 1, alignSelf: 'start' }}>
          <Box aria-hidden="true" sx={{
            bgcolor: 'action.hover', borderRadius: 999, position: 'relative', height: 8, overflow: 'hidden',
          }}>
            {receivedShare !== null && (
              <>
                <Box sx={{ bgcolor: SENT_COLOR, position: 'absolute', inset: 0 }} />
                <Box sx={{ bgcolor: RECEIVED_COLOR, position: 'absolute', inset: 0, clipPath: `inset(0 ${100 - receivedShare}% 0 0)` }} />
              </>
            )}
          </Box>
          <Stack direction="row" sx={{ columnGap: 3, flexWrap: 'wrap', mt: 1.5, rowGap: 1 }}>
            <SplitLegend color={RECEIVED_COLOR} label="Received" value={isLoading ? undefined : received} />
            <SplitLegend color={SENT_COLOR} label="Sent" value={isLoading ? undefined : sent} />
          </Stack>
        </Box>
      </Stat>
    </Panel>
  );
}
