import { Box, LinearProgress, Skeleton, Stack, Typography } from '@mui/material';
import { Children, useId } from 'react';

import { Panel } from '../ui/Panel';

export interface OverviewProgress {
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

function SplitLegend({ color, label, value }: { color: string; label: string; value: string }) {
  return (
    <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', minWidth: 0 }}>
      <Box aria-hidden="true" sx={{
        alignSelf: 'center', bgcolor: color, borderRadius: '50%', flexShrink: 0, height: 8, width: 8,
      }} />
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>{label}</Typography>
      <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums', fontWeight: 600 }}>
        {value}
      </Typography>
    </Stack>
  );
}

const RECEIVED_COLOR = 'measurement.main';
const SENT_COLOR = 'communication.outgoingBorder';

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
        gridTemplateRows: { md: 'auto auto auto' }, p: { xs: 2.5, md: 3.5 },
      }}
    >
      <Stat label="Conversations">
        {isLoading ? (
          <Skeleton variant="text" width={120} sx={{ typography: 'kpi' }} />
        ) : (
          <Typography variant="kpi" data-visual="conversation-total">{conversations}</Typography>
        )}
        {progress && (
          <Stack data-journey-state="desktop.history_syncing" spacing={1} sx={{ maxWidth: 280, mt: 2.5 }}>
            <LinearProgress
              aria-label={progress.label}
              variant={progress.percent === null ? 'indeterminate' : 'determinate'}
              value={progress.percent ?? undefined}
            />
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>{progress.label}</Typography>
          </Stack>
        )}
      </Stat>
      <Stat label="Messages" divider>
        {isLoading ? (
          <Skeleton variant="text" width={100} sx={{ typography: 'metric' }} />
        ) : (
          <Typography variant="metric" data-visual="message-total">{messages}</Typography>
        )}
        <Box sx={{ minWidth: 0, mt: 1 }}>
          <Box aria-hidden="true" sx={{
            bgcolor: 'action.hover', borderRadius: 999, display: 'flex', height: 8, overflow: 'hidden',
          }}>
            {receivedShare !== null && (
              <>
                <Box sx={{ bgcolor: RECEIVED_COLOR, width: `${receivedShare}%` }} />
                <Box sx={{ bgcolor: SENT_COLOR, flex: 1, ml: '2px' }} />
              </>
            )}
          </Box>
          <Stack direction="row" sx={{ columnGap: 3, flexWrap: 'wrap', mt: 1.5, rowGap: 1 }}>
            <SplitLegend color={RECEIVED_COLOR} label="Received" value={isLoading ? '…' : received} />
            <SplitLegend color={SENT_COLOR} label="Sent" value={isLoading ? '…' : sent} />
          </Stack>
        </Box>
      </Stat>
    </Panel>
  );
}
