import { Box, Chip, type ChipProps } from '@mui/material';

export type StatusTone = 'default' | 'error' | 'info' | 'success' | 'warning';

export type StatusChipProps = Omit<ChipProps, 'color' | 'icon' | 'variant'> & {
  tone?: StatusTone;
};

/** Outlined chip whose colored dot carries the tone, so the label stays short. */
export function StatusChip({ sx, tone = 'default', ...props }: StatusChipProps) {
  return (
    <Chip
      {...props}
      icon={(
        <Box
          component="span"
          sx={(theme) => ({
            bgcolor:
              tone === 'default' ? theme.vars.palette.text.disabled : theme.vars.palette[tone].main,
            borderRadius: '50%',
            flexShrink: 0,
            height: 8,
            width: 8,
          })}
        />
      )}
      variant="outlined"
      sx={[
        {
          bgcolor: 'background.paper',
          borderColor: 'divider',
          flexShrink: 0,
          '& .MuiChip-icon': { ml: props.size === 'small' ? 1 : 1.25, mr: 0.25 },
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    />
  );
}
