import { Box, Chip, type ChipProps } from '@mui/material';

import { componentTokens } from '../../theme/generated/tokens';
import { statusSettled } from '../../theme/presentationMotion';

export type StatusTone = 'default' | 'error' | 'info' | 'success' | 'warning';
export type StatusChipProps = Omit<ChipProps, 'color' | 'icon' | 'variant'> & {
  tone?: StatusTone;
  settled?: boolean;
};

/** Neutral field with a labeled feedback dot; live announcements belong to the parent. */
export function StatusChip({ sx, tone = 'default', settled = false, ...props }: StatusChipProps) {
  const size = props.size ?? 'small';
  const healthy = settled && tone === 'success';
  const dotSize = healthy ? componentTokens.StatusChip.settledDotSize : componentTokens.StatusChip.dotSize;
  return (
    <Chip
      {...props}
      size={size}
      icon={(
        <Box
          component="span"
          key={healthy ? 'settled' : 'status'}
          data-status-settled={healthy ? 'true' : undefined}
          aria-hidden="true"
          sx={(theme) => ({
            bgcolor: tone === 'default' ? theme.vars.palette.text.disabled : theme.vars.palette[tone].main,
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
