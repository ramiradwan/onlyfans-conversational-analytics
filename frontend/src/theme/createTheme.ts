import type { AlertProps } from '@mui/material/Alert';
import { createTheme, type PaletteOptions, type Theme } from '@mui/material/styles';
import type {} from '@mui/material/themeCssVarsAugmentation';

import {
  brandPalette,
  brandTypography,
  componentTokens,
  effectTokens,
  layoutTokens,
  semanticColorSchemes,
  semanticIntents,
  shape,
  typography,
} from './generated/tokens';

type SchemeTokens =
  | typeof semanticIntents.light
  | typeof semanticIntents.dark;

const { duration, easing, pressScale } = effectTokens.motion;

function milliseconds(value: string): number {
  return Number.parseInt(value, 10);
}

/** Transition shorthand for the given properties on one motion step. */
function transition(properties: readonly string[], step: keyof typeof duration = 'fast'): string {
  return properties
    .map((property) => `${property} ${duration[step]} ${easing.standard}`)
    .join(', ');
}

// MUI adapts Bridge intents; compatibility names retain their existing values.
function buildPalette(scheme: SchemeTokens, contrastThreshold: number): PaletteOptions {
  return {
    contrastThreshold,
    primary: scheme.action.primary,
    secondary: scheme.action.secondary,
    accent: scheme.legacy.accent,
    calm: scheme.legacy.calm,
    success: scheme.feedback.success,
    warning: scheme.feedback.warning,
    error: scheme.feedback.error,
    info: scheme.feedback.info,
    measurement: scheme.measurement,
    brand: scheme.brand,
    sentiment: scheme.sentiment,
    background: { default: scheme.surface.canvas, paper: scheme.surface.paper },
    text: scheme.text,
    divider: scheme.surface.divider,
    action: scheme.action.state,
    placeholder: scheme.surface.placeholder,
    surface: {
      subtle: scheme.surface.subtle, glass: scheme.surface.glass,
      elevation: scheme.surface.elevation, overlay: scheme.surface.overlay,
      rim: scheme.surface.rim, glow: scheme.surface.glow,
      dominant: scheme.surface.dominant,
      tooltip: scheme.surface.tooltip,
      avatar: scheme.surface.avatar,
      trust: scheme.surface.trust,
      segment: scheme.surface.segment,
      error: scheme.surface.error, metric: scheme.surface.metric,
    },
    communication: scheme.communication,
    chart: scheme.chart,
  };
}

function rim(theme: Theme) {
  return {
    '&::before': {
      background: theme.vars.palette.surface.rim,
      borderRadius: 'inherit',
      content: '""',
      inset: 0,
      mask: 'linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0)',
      maskComposite: 'exclude',
      padding: effectTokens.borders.thin,
      pointerEvents: 'none',
      position: 'absolute' as const,
    },
  };
}

/** Opaque content surface: tonal separation from the canvas with a hairline rim. */
function surfaceEffect(theme: Theme) {
  return {
    border: 'none',
    borderRadius: `${componentTokens.MuiPaper.borderRadius}px`,
    boxShadow: theme.vars.palette.surface.elevation,
    position: 'relative' as const,
    ...rim(theme),
  };
}

/** Floating layer above content: menus, popovers, dialogs and navigation chrome. */
function overlayEffect(theme: Theme) {
  return {
    boxShadow: theme.vars.palette.surface.overlay,
    ...rim(theme),
  };
}

const focusRing = (theme: Theme) => ({
  outline: effectTokens.focus.width + ' solid ' + theme.vars.palette.primary.main,
  outlineOffset: effectTokens.focus.offset,
});

/** Standard alert: a paper surface tinted by the feedback tone, with body text at full contrast. */
function feedbackSurface(theme: Theme, tone: NonNullable<AlertProps['severity']>) {
  const main = theme.vars.palette[tone].main;
  const paper = theme.vars.palette.background.paper;
  const { toneBorder, toneFill } = componentTokens.MuiAlert;
  // Mixing with transparent keeps the tone's hue; the paper beneath keeps the surface opaque.
  const fill = `color-mix(in oklch, ${main} ${toneFill}, transparent)`;
  return {
    backgroundColor: paper,
    backgroundImage: `linear-gradient(${fill}, ${fill})`,
    border: `${effectTokens.borders.thin} solid color-mix(in oklch, ${main} ${toneBorder}, transparent)`,
    color: theme.vars.palette.text.primary,
    '& .MuiAlert-icon': { color: main },
  };
}

/** ThemeProvider color scheme props; the first-paint head script in the HTML entries mirrors them. */
export const colorSchemeProps = { defaultMode: 'light' } as const;

export const theme = createTheme({
  cssVariables: {
    cssVarPrefix: 'bridge',
    colorSchemeSelector: 'data-mui-color-scheme',
    nativeColor: true,
  },
  brandPalette,
  brandTypography,
  colorSchemes: {
    light: { palette: buildPalette(semanticIntents.light, semanticColorSchemes.light.contrastThreshold) },
    dark: { palette: buildPalette(semanticIntents.dark, semanticColorSchemes.dark.contrastThreshold) },
  },
  spacing: layoutTokens.spacingUnit,
  breakpoints: { values: layoutTokens.breakpoints },
  zIndex: layoutTokens.zIndex,
  shape,
  typography,
  transitions: {
    duration: {
      shortest: milliseconds(duration.fast),
      shorter: milliseconds(duration.fast),
      short: milliseconds(duration.standard),
      standard: milliseconds(duration.standard),
      complex: milliseconds(duration.spatial),
      enteringScreen: milliseconds(duration.standard),
      leavingScreen: milliseconds(duration.fast),
    },
    easing: {
      easeInOut: easing.standard,
      easeOut: easing.enter,
      easeIn: easing.exit,
      sharp: easing.standard,
    },
  },
  effects: {
    ambientGlow: (theme: Theme) => ({
      backgroundImage: theme.vars.palette.surface.glow,
    }),
    cardBorder: surfaceEffect,
    chartFrame: surfaceEffect,
    glassmorphism: (theme: Theme) => ({
      backdropFilter: effectTokens.glassmorphism.backdropFilter,
      backgroundColor: theme.vars.palette.surface.glass,
      WebkitBackdropFilter: effectTokens.glassmorphism.backdropFilter,
    }),
    headerBorder: (theme: Theme) => ({
      boxShadow: `inset 0 -${effectTokens.borders.thin} 0 ${theme.vars.palette.divider}`,
    }),
    overlay: overlayEffect,
    sideBorder: overlayEffect,
  },
  components: {
    MuiAlert: {
      styleOverrides: {
        standard: ({ ownerState, theme }: { ownerState: AlertProps; theme: Theme }) =>
          feedbackSurface(theme, ownerState.color ?? ownerState.severity ?? 'success'),
      },
    },
    MuiCollapse: {
      defaultProps: {
        timeout: {
          enter: milliseconds(duration.standard),
          exit: milliseconds(duration.fast),
        },
        easing: {
          enter: easing.enter,
          exit: easing.exit,
        },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiButton.borderRadius}px`,
          transition: transition(['background-color', 'border-color', 'color', 'transform']),
          '&:active': {
            transform: `scale(${componentTokens.MuiButton.activeScale})`,
          },
          '&:focus-visible': focusRing(theme),
        }),
        sizeLarge: { minHeight: 48, paddingInline: 22 },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          backgroundImage: 'none',
          ...surfaceEffect(theme),
        }),
      },
    },
    MuiChip: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          fontWeight: brandTypography.weights.medium,
          '&:focus-visible': focusRing(theme),
        }),
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiPaper.borderRadius}px`,
          ...overlayEffect(theme),
        }),
      },
    },
    MuiIconButton: {
      // The authored focus field and ring replace MUI's animated grey keyboard ripple.
      defaultProps: { disableFocusRipple: true },
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiButton.borderRadius}px`,
          transition: transition(['background-color', 'color', 'transform']),
          '&:active': {
            transform: `scale(${pressScale})`,
          },
          '&:focus-visible, &.Mui-focusVisible': { ...focusRing(theme), backgroundColor: theme.vars.palette.action.focus },
        }),
      },
    },
    MuiLinearProgress: {
      styleOverrides: {
        root: { borderRadius: layoutTokens.radius.pill, height: 6 },
        bar: { borderRadius: layoutTokens.radius.pill },
      },
    },
    MuiListItemButton: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiListItemButton.borderRadius}px`,
          transition: transition(['background-color', 'color', 'transform']),
          '&:active': {
            transform: `scale(${componentTokens.MuiListItemButton.activeScale})`,
          },
          '&:focus-visible, &.Mui-focusVisible': {
            backgroundColor: theme.vars.palette.action.focus,
            ...focusRing(theme),
            outlineOffset: '-' + effectTokens.focus.offset,
          },
        }),
      },
    },
    MuiPaper: {
      defaultProps: { elevation: 0 },
      styleOverrides: {
        root: { backgroundImage: 'none' },
        rounded: { borderRadius: `${componentTokens.MuiPaper.borderRadius}px` },
        outlined: ({ theme }: { theme: Theme }) => ({
          border: effectTokens.borders.thin + ' solid ' + theme.vars.palette.divider,
        }),
      },
    },
    MuiPopover: {
      styleOverrides: {
        paper: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiMenu.borderRadius}px`,
          ...overlayEffect(theme),
        }),
      },
    },
    MuiSkeleton: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          backgroundColor: theme.vars.palette.placeholder,
        }),
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: { fontVariantNumeric: brandTypography.numeric },
      },
    },
    MuiToggleButtonGroup: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          backgroundColor: theme.vars.palette.surface.segment.track,
          border: `1px solid ${theme.vars.palette.divider}`,
          borderRadius: `${componentTokens.segmentedControl.trackRadius}px`,
          padding: componentTokens.segmentedControl.padding,
          gap: componentTokens.segmentedControl.padding,
          '& .MuiToggleButton-root': {
            border: 0, margin: 0,
            borderRadius: `${componentTokens.segmentedControl.segmentRadius}px`,
          },
        }),
      },
    },
    MuiToggleButton: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          ...theme.typography.caption,
          fontWeight: brandTypography.weights.medium,
          color: theme.vars.palette.text.secondary,
          textTransform: 'none',
          '&:hover': { backgroundColor: theme.vars.palette.action.hover },
          '&.Mui-selected, &.Mui-selected:hover': {
            backgroundColor: theme.vars.palette.surface.segment.selected,
            color: theme.vars.palette.text.primary,
            boxShadow: theme.vars.palette.surface.segment.elevation,
            fontWeight: brandTypography.weights.semibold,
          },
          '&:focus-visible, &.Mui-focusVisible': { ...focusRing(theme), outlineOffset: '-2px' },
          '&.Mui-disabled': { color: theme.vars.palette.text.disabled },
        }),
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiTooltip.borderRadius}px`,
          backgroundColor: theme.vars.palette.surface.tooltip.fill,
          color: theme.vars.palette.surface.tooltip.text,
          fontSize: theme.typography.caption.fontSize,
          fontWeight: brandTypography.weights.medium,
        }),
      },
    },
    MuiTypography: {
      defaultProps: {
        variantMapping: { kpi: 'p', metric: 'p', insight: 'span', metricUnit: 'span', numericCaption: 'span', numericBody: 'span', detailLabel: 'span', tableHeading: 'span', passkeyTitle: 'h1' },
      },
    },
    MuiCssBaseline: {
      styleOverrides: {
        '@media (prefers-reduced-motion: reduce)': {
          '*, *::before, *::after': {
            animationDuration: '0.01ms !important',
            animationIterationCount: '1 !important',
            scrollBehavior: 'auto !important',
            transitionDuration: '0.01ms !important',
          },
        },
      },
    },
  },
});
