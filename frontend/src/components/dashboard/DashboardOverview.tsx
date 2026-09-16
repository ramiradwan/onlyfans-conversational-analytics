import { Box, LinearProgress, Paper, Skeleton, Stack, Typography } from '@mui/material';
import { useId } from 'react';

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
  /** Raw counts for the received/sent proportion bar; omitted when either is unknown. */
  split?: { received: number; sent: number } | null;
}

function Stat({
  children,
  label,
}: {
  children: React.ReactNode;
  label: string;
}) {
  const labelId = useId();
  return (
    <Box role="group" aria-labelledby={labelId} sx={{ minWidth: 0 }}>
      <Typography id={labelId} variant="body2" sx={{ color: 'text.secondary', mb: 0.75 }}>
        {label}
      </Typography>
      {children}
    </Box>
  );
}

function SplitLegend({ color, label, value }: { color: string; label: string; value: string }) {
  return (
    <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', minWidth: 0 }}>
      <Box
        aria-hidden="true"
        sx={{ alignSelf: 'center', bgcolor: color, borderRadius: '50%', flexShrink: 0, height: 8, width: 8 }}
      />
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums', fontWeight: 600 }}>
        {value}
      </Typography>
    </Stack>
  );
}

const RECEIVED_COLOR = 'primary.main';
const SENT_COLOR = 'communication.outgoingBorder';

/** Headline counts: one dominant conversation total with the message volume and its direction split. */
export function DashboardOverview({
  conversations,
  isLoading = false,
  messages,
  progress = null,
  received,
  sent,
  split = null,
}: DashboardOverviewProps) {
  const total = split ? split.received + split.sent : 0;
  const receivedShare = total > 0 && split ? (split.received / total) * 100 : null;

  return (
    <Paper
      component="section"
      aria-busy={isLoading}
      aria-label="Overview"
      sx={(theme) => ({
        display: 'grid',
        gap: { xs: 3, md: 5 },
        gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'minmax(0, 2fr) minmax(0, 3fr)' },
        p: { xs: 2.5, md: 3.5 },
        ...theme.effects.cardBorder(theme),
      })}
    >
      <Stat label="Conversations">
        {isLoading ? (
          <Skeleton variant="text" width={120} sx={{ fontSize: '3rem' }} />
        ) : (
          <Typography variant="kpi">{conversations}</Typography>
        )}
        {progress && (
          <Stack spacing={1} sx={{ maxWidth: 280, mt: 2.5 }}>
            <LinearProgress
              aria-label={progress.label}
              variant={progress.percent === null ? 'indeterminate' : 'determinate'}
              value={progress.percent ?? undefined}
            />
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              {progress.label}
            </Typography>
          </Stack>
        )}
      </Stat>

      <Box
        sx={(theme) => ({
          [theme.breakpoints.up('md')]: {
            borderInlineStart: `1px solid ${theme.vars.palette.divider}`,
            pl: 5,
          },
        })}
      >
        <Stat label="Messages">
          {isLoading ? (
            <Skeleton variant="text" width={100} sx={{ fontSize: '1.5rem' }} />
          ) : (
            <Typography variant="metric">{messages}</Typography>
          )}
        </Stat>
        <Box
          aria-hidden="true"
          sx={{
            bgcolor: 'action.hover',
            borderRadius: 999,
            display: 'flex',
            height: 8,
            mt: 2,
            overflow: 'hidden',
          }}
        >
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
    </Paper>
  );
}
