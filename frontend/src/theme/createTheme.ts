import { createTheme, type PaletteOptions, type Theme } from '@mui/material/styles';
import type {} from '@mui/material/themeCssVarsAugmentation';

import {
  brandPalette,
  brandTypography,
  componentTokens,
  effectTokens,
  layoutTokens,
  semanticColorSchemes,
  shape,
  typography,
} from './generated/tokens';

type SchemeTokens =
  | typeof semanticColorSchemes.light
  | typeof semanticColorSchemes.dark;

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

// Bridge owns light/main/dark intent tones; nativeColor remains enabled for runtime CSS color operations.
function buildPalette(scheme: SchemeTokens): PaletteOptions {
  return {
    contrastThreshold: scheme.contrastThreshold,
    primary: scheme.primary,
    secondary: scheme.secondary,
    accent: scheme.accent,
    calm: scheme.calm,
    success: scheme.success,
    warning: scheme.warning,
    error: scheme.error,
    info: scheme.info,
    background: scheme.background,
    text: scheme.text,
    divider: scheme.divider,
    action: scheme.action,
    placeholder: scheme.placeholder,
    surface: scheme.surface,
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

export const theme = createTheme({
  cssVariables: {
    cssVarPrefix: 'bridge',
    colorSchemeSelector: 'data-mui-color-scheme',
    nativeColor: true,
  },
  brandPalette,
  brandTypography,
  colorSchemes: {
    light: { palette: buildPalette(semanticColorSchemes.light) },
    dark: { palette: buildPalette(semanticColorSchemes.dark) },
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
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiButton.borderRadius}px`,
          transition: transition(['background-color', 'color', 'transform']),
          '&:active': {
            transform: `scale(${pressScale})`,
          },
          '&:focus-visible': focusRing(theme),
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
          '&:focus-visible': {
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
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          borderRadius: `${componentTokens.MuiTooltip.borderRadius}px`,
          fontWeight: brandTypography.weights.medium,
        },
      },
    },
    MuiTypography: {
      defaultProps: {
        variantMapping: { kpi: 'p', metric: 'p' },
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
