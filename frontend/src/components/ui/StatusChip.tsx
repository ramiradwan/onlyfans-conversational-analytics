import { Box, Chip, type ChipProps, type Theme } from '@mui/material';

import { componentTokens } from '../../theme/generated/tokens';
import { statusSettled } from '../../theme/presentationMotion';

export type StatusTone = 'default' | 'error' | 'info' | 'success' | 'warning';
export type StatusChipProps = Omit<ChipProps, 'color' | 'icon' | 'variant'> & {
  longestLabel?: string;
  tone?: StatusTone;
  settled?: boolean;
};

/** Neutral field with a labeled feedback dot; live announcements belong to the parent. */
export function StatusChip({ label, longestLabel, sx, tone = 'default', settled = false, ...props }: StatusChipProps) {
  const size = props.size ?? 'small';
  const healthy = settled && tone === 'success';
  const dotSize = healthy ? componentTokens.StatusChip.settledDotSize : componentTokens.StatusChip.dotSize;
  const dotColor = (theme: Theme) =>
    tone === 'default' ? theme.vars.palette.text.disabled : theme.vars.palette[tone].main;
  return (
    <Chip
      {...props}
      size={size}
      label={longestLabel ? (
        <Box component="span" sx={{
          display: 'grid',
          textAlign: 'center',
          // The widest label sets the size while the visible label stays centered.
          '&::before': {
            content: `${JSON.stringify(longestLabel)} / ""`,
            gridArea: '1 / 1',
            paddingInlineStart: `calc(${dotSize}px + 0.5em)`,
            visibility: 'hidden',
          },
        }}>
          <Box component="span" sx={{ gridArea: '1 / 1', position: 'relative' }}>
            <Box component="span" sx={{
              position: 'absolute',
              left: '50%',
              transform: 'translateX(-50%)',
              '&::before': {
                content: '""',
                display: 'inline-block',
                verticalAlign: 'middle',
                marginInlineEnd: '0.5em',
                bgcolor: dotColor,
                borderRadius: '50%',
                width: dotSize,
                height: dotSize,
              },
            }}>{label}</Box>
          </Box>
        </Box>
      ) : label}
      icon={longestLabel ? undefined : (
        <Box
          component="span"
          key={healthy ? 'settled' : 'status'}
          data-status-settled={healthy ? 'true' : undefined}
          aria-hidden="true"
          sx={(theme) => ({
            bgcolor: dotColor(theme),
            borderRadius: '50%', flexShrink: 0,
            height: dotSize,
            width: dotSize,
            ...(healthy ? statusSettled : {}),
          })}
        />
      )}
      variant="outlined"
      sx={[
        {
          bgcolor: 'background.paper', borderColor: 'divider', color: 'text.secondary',
          borderRadius: componentTokens.StatusChip.borderRadius,
          fontSize: componentTokens.StatusChip.fontSize, flexShrink: 0,
          '& .MuiChip-icon': { ml: size === 'small' ? 1 : 1.25, mr: 0.25 },
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    />
  );
}
