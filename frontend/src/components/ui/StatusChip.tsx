import { Box, Chip, type ChipProps } from '@mui/material';

import { componentTokens } from '../../theme/generated/tokens';

export type StatusTone = 'default' | 'error' | 'info' | 'success' | 'warning';
export type StatusChipProps = Omit<ChipProps, 'color' | 'icon' | 'variant'> & {
  tone?: StatusTone;
};

/** Neutral field with a labeled feedback dot; live announcements belong to the parent. */
export function StatusChip({ sx, tone = 'default', ...props }: StatusChipProps) {
  const size = props.size ?? 'small';
  return (
    <Chip
      {...props}
      size={size}
      icon={(
        <Box
          component="span"
          aria-hidden="true"
          sx={(theme) => ({
            bgcolor: tone === 'default' ? theme.vars.palette.text.disabled : theme.vars.palette[tone].main,
            borderRadius: '50%', flexShrink: 0,
            height: componentTokens.StatusChip.dotSize,
            width: componentTokens.StatusChip.dotSize,
          })}
        />
      )}
      variant="outlined"
      sx={[
        {
          bgcolor: 'surface.subtle', borderColor: 'transparent', color: 'text.primary',
          borderRadius: componentTokens.StatusChip.borderRadius,
          fontSize: componentTokens.StatusChip.fontSize, flexShrink: 0,
          '& .MuiChip-icon': { ml: size === 'small' ? 1 : 1.25, mr: 0.25 },
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    />
  );
}
