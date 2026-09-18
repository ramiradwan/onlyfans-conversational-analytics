import { Box, Stack, Typography, type SxProps, type Theme } from '@mui/material';
import type { ReactNode } from 'react';

import { StatusChip, type StatusTone } from './StatusChip';

export interface SectionStatus {
  label: string;
  tone: StatusTone;
}

/** Settings card heading: title, one-line summary, and a status chip announced on change. */
export function SectionHeader({
  status,
  sx,
  summary,
  title,
}: {
  status?: SectionStatus | null;
  sx?: SxProps<Theme>;
  summary?: ReactNode;
  title: string;
}) {
  return (
    <Stack direction="row" spacing={2} sx={[{ alignItems: 'flex-start', justifyContent: 'space-between' }, ...(Array.isArray(sx) ? sx : [sx])]}>
      <Box sx={{ minWidth: 0 }}>
        <Typography component="h2" variant="h6">
          {title}
        </Typography>
        {summary && (
          <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.25 }}>
            {summary}
          </Typography>
        )}
      </Box>
      <Box aria-live="polite" sx={{ flexShrink: 0 }}>
        {status && <StatusChip label={status.label} size="small" tone={status.tone} />}
      </Box>
    </Stack>
  );
}

/** One setting: label and short description, with its action aligned to the end. */
export function SettingRow({
  action,
  sx,
  description,
  title,
}: {
  action?: ReactNode;
  sx?: SxProps<Theme>;
  description?: ReactNode;
  title: string;
}) {
  return (
    <Stack
      direction={{ xs: 'column', sm: 'row' }}
      spacing={{ xs: 1.5, sm: 3 }}
      sx={[{ alignItems: { sm: 'center' }, justifyContent: 'space-between' }, ...(Array.isArray(sx) ? sx : [sx])]}
    >
      <Box sx={{ minWidth: 0 }}>
        <Typography component="h3" variant="subtitle2">
          {title}
        </Typography>
        {description && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            {description}
          </Typography>
        )}
      </Box>
      {action && <Box sx={{ flexShrink: 0 }}>{action}</Box>}
    </Stack>
  );
}
