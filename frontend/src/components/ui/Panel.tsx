import { Paper, type PaperProps } from '@mui/material';

export type PanelEmphasis = 'dominant' | 'secondary' | 'quiet';
export type PanelProps = PaperProps & { emphasis?: PanelEmphasis };

/** Existing content surface with an explicit place in the page hierarchy. */
export function Panel({ emphasis = 'secondary', sx, ...props }: PanelProps) {
  return (
    <Paper
      {...props}
      elevation={0}
      data-surface-emphasis={emphasis}
      sx={[
        (theme) => ({
          p: 3, bgcolor: 'background.paper', display: 'flex',
          flexDirection: 'column', gap: 2, minWidth: 0,
          ...theme.effects.cardBorder(theme),
          ...(emphasis === 'dominant' ? {
            border: `1px solid ${theme.vars.palette.surface.dominant.border}`,
            boxShadow: theme.vars.palette.surface.dominant.elevation,
            '&::before': { display: 'none' },
          } : {}),
          ...(emphasis === 'quiet' ? {
            bgcolor: 'transparent', border: 0, boxShadow: 'none',
            '&::before': { display: 'none' },
          } : {}),
        }),
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    />
  );
}
