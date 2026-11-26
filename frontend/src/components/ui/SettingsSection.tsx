import { Box, Stack, Typography, type SxProps, type Theme, type TypographyProps } from '@mui/material';
import type { ReactNode } from 'react';

import { StatusChip, type StatusTone } from './StatusChip';

export interface SectionStatus {
  label: string;
  tone: StatusTone;
}

/** Settings card heading: title, one-line summary, and a status chip announced on change. */
export function SectionHeader({
  headingProps,
  status,
  statusMeasure,
  sx,
  summary,
  title,
}: {
  headingProps?: Pick<TypographyProps<'h2'>, 'id' | 'ref' | 'sx' | 'tabIndex'>;
  status?: SectionStatus | null;
  statusMeasure?: string;
  sx?: SxProps<Theme>;
  summary?: ReactNode;
  title: string;
}) {
  return (
    <Box sx={{ containerType: 'inline-size' }}>
      <Box data-settings-section-header sx={[{
        display: 'grid',
        gridTemplateColumns: status === undefined ? 'minmax(0, 1fr)' : 'minmax(0, 1fr) min(50%, 10rem)',
        alignItems: 'start',
        columnGap: 1,
        minBlockSize: status === undefined ? undefined : '1.5rem',
        '@container (max-width: 14rem)': { gridTemplateColumns: 'minmax(0, 1fr)' },
      }, ...(Array.isArray(sx) ? sx : [sx])]}>
        <Box sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
          <Typography component="h2" variant="h6" {...headingProps}>
            {title}
          </Typography>
          {summary && (
            <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.25 }}>
              {summary}
            </Typography>
          )}
        </Box>
        {status !== undefined && (
          <Box aria-live="polite" sx={{ display: 'flex', justifyContent: 'flex-end', minWidth: 0 }}>
            {status && <StatusChip
              data-settings-status
              label={status.label}
              longestLabel={statusMeasure}
              size="small"
              tone={status.tone}
              sx={{ height: '1.5rem' }} />}
          </Box>
        )}
      </Box>
    </Box>
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
