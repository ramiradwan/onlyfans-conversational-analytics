import { keyframes } from '@emotion/react';
import { Box, Divider, Paper, Stack, Typography } from '@mui/material';
import type { ReactNode } from 'react';

import { Panel } from './Panel';
import { SettingRow } from './SettingsSection';
import { componentTokens, layoutTokens } from '../../theme';

const calmBreathe = keyframes({
  '0%, 100%': { opacity: 0.28 },
  '50%': { opacity: 0.85 },
});

export function LoadingDot() {
  return (
    <Box
      component="span"
      aria-hidden="true"
      sx={(theme) => ({
        animation: `${calmBreathe} 2.4s ease-in-out infinite`,
        bgcolor: theme.vars.palette.measurement.main,
        borderRadius: '50%',
        display: 'inline-block',
        flexShrink: 0,
        height: 6,
        width: 6,
        '@media (prefers-reduced-motion: reduce)': {
          animation: 'none',
          opacity: 0.6,
        },
      })}
    />
  );
}

export function LoadingCaption({
  children,
  variant = 'body2',
}: {
  children: ReactNode;
  variant?: 'body2' | 'caption';
}) {
  return (
    <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
      <LoadingDot />
      <Typography variant={variant} sx={{ color: 'text.secondary' }}>
        {children}
      </Typography>
    </Stack>
  );
}

function QuietBar({
  height,
  width,
  radius = 4,
}: {
  height: number;
  width: number | string;
  radius?: number;
}) {
  return (
    <Box
      component="span"
      aria-hidden="true"
      sx={{
        bgcolor: 'placeholder',
        borderRadius: `${radius}px`,
        display: 'block',
        flexShrink: 0,
        height,
        width,
      }}
    />
  );
}

export function WorkspaceRouteSkeleton() {
  return (
    <Box
      role="status"
      aria-live="polite"
      aria-label="Loading workspace"
      sx={{
        bgcolor: 'background.default',
        display: 'grid',
        minHeight: '100dvh',
        placeItems: 'center',
        px: 3,
        width: '100%',
      }}
    >
      <Stack spacing={1} sx={{ alignItems: 'center', textAlign: 'center' }}>
        <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center' }}>
          <LoadingDot />
          <Typography variant="subtitle1" sx={{ color: 'text.primary' }}>
            Loading workspace
          </Typography>
        </Stack>
      </Stack>
    </Box>
  );
}

const RECENT_ROWS = [
  { name: 118, preview: '56%', opacity: 0.75 },
  { name: 102, preview: '68%', opacity: 0.42 },
  { name: 128, preview: '46%', opacity: 0.16 },
] as const;

export function RecentConversationsSkeleton({ label = 'Loading recent conversations…' }: { label?: string }) {
  const row = componentTokens.RecentConversations;
  return (
    <Paper
      component="section"
      aria-busy="true"
      aria-label={label}
      elevation={0}
      sx={(theme) => ({
        bgcolor: 'background.paper',
        overflow: 'hidden',
        width: '100%',
        ...theme.effects.cardBorder(theme),
      })}
    >
      <Box
        sx={{
          alignItems: 'center',
          borderBottom: '1px solid',
          borderColor: 'divider',
          display: 'flex',
          justifyContent: 'space-between',
          px: { xs: 2.5, md: 3 },
          py: 1.75,
        }}
      >
        <Typography component="h2" variant="h6">
          Recent conversations
        </Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          Loading…
        </Typography>
      </Box>
      <Stack divider={<Divider />}>
        {RECENT_ROWS.map((item, index) => (
          <Stack
            key={index}
            direction="row"
            data-recent-placeholder-row
            spacing={2}
            sx={{ alignItems: 'center', minHeight: '4.5rem', opacity: item.opacity, px: { xs: 2.5, md: 3 }, py: 1.5 }}
          >
            <Box
              aria-hidden="true"
              sx={{
                bgcolor: 'placeholder',
                borderRadius: `${row.avatarRadius}px`,
                flexShrink: 0,
                height: row.avatarSize,
                width: row.avatarSize,
              }}
            />
            <Stack spacing={0.85} sx={{ flex: 1, minWidth: 0 }}>
              <QuietBar width={item.name} height={11} />
              <QuietBar width={item.preview} height={9} />
            </Stack>
          </Stack>
        ))}
      </Stack>
    </Paper>
  );
}

export function SettingsSectionSkeleton({
  title,
  statusLabel,
}: {
  title?: string;
  statusLabel?: string;
}) {
  return (
    <Stack
      aria-busy="true"
      data-visual="settings-loading"
      direction="row"
      spacing={2}
      sx={{ alignItems: 'baseline', justifyContent: 'space-between', px: 3, py: 2.5 }}
    >
      {title ? (
        <Typography component="p" variant="h6">
          {title}
        </Typography>
      ) : (
        <QuietBar width={148} height={15} />
      )}
      <Typography role={statusLabel ? 'status' : undefined} variant="caption" sx={{ color: 'text.secondary' }}>
        {statusLabel ?? 'Loading…'}
      </Typography>
    </Stack>
  );
}

export function FindingsTableSkeleton() {
  return (
    <Stack role="status" aria-label="Loading saved literal values" spacing={1.5} divider={<Divider />}>
      <LoadingCaption>Loading saved literal values…</LoadingCaption>
      {['58%', '45%', '51%'].map((width) => (
        <Stack key={width} direction="row" spacing={2} sx={{ alignItems: 'center', py: 1 }}>
          <QuietBar width={width} height={12} />
          <QuietBar width="6ch" height={12} />
        </Stack>
      ))}
    </Stack>
  );
}

export function VaultRowsSkeleton() {
  return (
    <Stack aria-busy="true" aria-label="Loading stored message settings">
      {['Archive', 'Download a copy', 'Delete messages'].map((title) => (
        <Box key={title}>
          <Divider />
          <SettingRow title={title} sx={{ px: 3, py: 2 }}
            description={<QuietBar width="min(100%, 16rem)" height={10} />}
            action={<QuietBar width={128} height={32} />} />
        </Box>
      ))}
    </Stack>
  );
}

const TOPIC_ROWS = [
  { label: 128, bar: '74%', opacity: 0.72 },
  { label: 104, bar: '48%', opacity: 0.4 },
  { label: 138, bar: '28%', opacity: 0.16 },
] as const;

export function AnalyticsSummarySkeleton({ message }: { message?: string }) {
  return (
    <Stack spacing={2} role="status" aria-live="polite" aria-label={message ?? 'Loading analytics'}>
      <Box
        sx={{
          display: 'grid',
          gap: 2,
          gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'minmax(0, 2fr) minmax(17rem, 1fr)' },
        }}
      >
        <Panel
          emphasis="dominant"
          data-visual="analytics-loading-primary"
          sx={{ gap: 2, p: 2.5 }}
        >
          <Stack spacing={0.5}>
            <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
              <Typography component="h2" variant="h6">
                Message tone over time
              </Typography>
              <LoadingCaption variant="caption">Loading…</LoadingCaption>
            </Stack>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              {message ?? 'Average tone of messages each day, from −1 (negative) to +1 (positive).'}
            </Typography>
          </Stack>
          <Box
            sx={{
              display: 'flex',
              flexDirection: 'column',
              height: componentTokens.analytics.chartHeight,
              justifyContent: 'space-between',
              py: 2,
              width: '100%',
            }}
          >
            <Box aria-hidden="true" sx={{ borderTop: '1px solid', borderColor: 'divider', opacity: 0.55 }} />
            <Box aria-hidden="true" sx={{ borderTop: '1px dashed', borderColor: 'divider', opacity: 0.9 }} />
            <Box aria-hidden="true" sx={{ borderTop: '1px solid', borderColor: 'divider', opacity: 0.55 }} />
          </Box>
        </Panel>
        <Panel data-visual="analytics-loading-replies" sx={{ gap: 2, p: 2.5 }}>
          <Stack spacing={0.5}>
            <Typography component="h2" variant="h6">
              Your replies
            </Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              How often and how quickly you reply.
            </Typography>
          </Stack>
          <Stack divider={<Divider />} spacing={2} sx={{ pt: 0.5 }}>
            <Stack spacing={0.5}>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Average reply time
              </Typography>
              <Typography variant="h4" sx={{ color: 'text.disabled', fontVariantNumeric: 'tabular-nums' }}>
                —
              </Typography>
            </Stack>
            <Stack spacing={1}>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Messages you replied to
              </Typography>
              <Typography variant="h4" sx={{ color: 'text.disabled', fontVariantNumeric: 'tabular-nums' }}>
                —
              </Typography>
              <Box
                aria-hidden="true"
                sx={{ bgcolor: 'surface.subtle', borderRadius: 999, height: 6, width: '100%' }}
              />
            </Stack>
            <Stack spacing={0.5}>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Turns per conversation
              </Typography>
              <Typography variant="h4" sx={{ color: 'text.disabled', fontVariantNumeric: 'tabular-nums' }}>
                —
              </Typography>
            </Stack>
          </Stack>
        </Panel>
        <Panel
          data-visual="analytics-loading-topics"
          sx={{ gap: 2, gridColumn: { md: '1 / -1' }, p: 2.5 }}
        >
          <Stack spacing={0.5}>
            <Typography component="h2" variant="h6">
              Topics
            </Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              What conversations are about.
            </Typography>
          </Stack>

          <Stack divider={<Divider />}>
            <Box
              sx={{
                color: 'text.secondary',
                display: 'grid',
                gap: 2,
                gridTemplateColumns: { xs: '1fr auto', sm: 'minmax(120px, 1.3fr) minmax(140px, 2fr) 72px 96px' },
                pb: 1.25,
              }}
            >
              <Typography variant="caption">Topic</Typography>
              <Typography variant="caption">Messages</Typography>
              <Typography variant="caption" sx={{ display: { xs: 'none', sm: 'block' }, textAlign: 'end' }}>
                Share
              </Typography>
              <Typography variant="caption" sx={{ display: { xs: 'none', sm: 'block' }, textAlign: 'end' }}>
                Change from previous period
              </Typography>
            </Box>
            {TOPIC_ROWS.map((topic, index) => (
              <Box
                key={index}
                sx={{
                  alignItems: 'center',
                  display: 'grid',
                  gap: 2,
                  gridTemplateColumns: { xs: '1fr auto', sm: 'minmax(120px, 1.3fr) minmax(140px, 2fr) 72px 96px' },
                  opacity: topic.opacity,
                  py: 1.5,
                }}
              >
                <QuietBar width={topic.label} height={11} />
                <Box
                  aria-hidden="true"
                  sx={{
                    bgcolor: 'surface.subtle',
                    borderRadius: `${layoutTokens.radius.pill}px`,
                    height: componentTokens.analytics.barThickness,
                    overflow: 'hidden',
                  }}
                >
                  <Box sx={{ bgcolor: 'placeholder', height: '100%', width: topic.bar }} />
                </Box>
                <Typography
                  variant="body2"
                  sx={{ color: 'text.disabled', display: { xs: 'none', sm: 'block' }, textAlign: 'end' }}
                >
                  —
                </Typography>
                <Typography
                  variant="body2"
                  sx={{ color: 'text.disabled', display: { xs: 'none', sm: 'block' }, textAlign: 'end' }}
                >
                  —
                </Typography>
              </Box>
            ))}
          </Stack>
        </Panel>
      </Box>
    </Stack>
  );
}
