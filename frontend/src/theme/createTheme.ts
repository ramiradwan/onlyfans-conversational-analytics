import { createTheme, type PaletteOptions, type Theme } from '@mui/material/styles';
import type {} from '@mui/material/themeCssVarsAugmentation';
import type {} from '@mui/x-data-grid/themeAugmentation';

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

function buildPalette(scheme: SchemeTokens): PaletteOptions {
  const seed = createTheme({
    palette: {
      mode: scheme.mode,
      contrastThreshold: scheme.contrastThreshold,
      tonalOffset: 0.2,
    },
  });
  const augment = (name: string, main: string) =>
    seed.palette.augmentColor({ color: { main }, name });

  return {
    contrastThreshold: scheme.contrastThreshold,
    primary: augment('primary', scheme.primary.main),
    secondary: augment('secondary', scheme.secondary.main),
    accent: augment('accent', scheme.accent.main),
    calm: augment('calm', scheme.calm.main),
    success: augment('success', scheme.success.main),
    warning: augment('warning', scheme.warning.main),
    error: augment('error', scheme.error.main),
    info: augment('info', scheme.info.main),
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

function surfaceEffect(theme: Theme) {
  return {
    border: 'none',
    borderRadius: `${componentTokens.MuiPaper.borderRadius}px`,
    boxShadow: theme.vars.palette.surface.elevation,
    position: 'relative' as const,
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

const focusRing = (theme: Theme) => ({
  outline: effectTokens.focus.width + ' solid ' + theme.vars.palette.primary.main,
  outlineOffset: effectTokens.focus.offset,
});

export const theme = createTheme({
  cssVariables: {
    cssVarPrefix: 'bridge',
    colorSchemeSelector: 'data-mui-color-scheme',
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
  effects: {
    cardBorder: surfaceEffect,
    chartFrame: surfaceEffect,
    glassmorphism: (theme: Theme) => ({
      backdropFilter: effectTokens.glassmorphism.backdropFilter,
      backgroundColor: theme.vars.palette.surface.glass,
      WebkitBackdropFilter: effectTokens.glassmorphism.backdropFilter,
    }),
    headerBorder: () => ({}),
    sideBorder: () => ({}),
  },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiButton.borderRadius}px`,
          transition:
            'transform ' + effectTokens.motion.duration + ' ' + effectTokens.motion.easing,
          '&:active': {
            transform: 'scale(' + componentTokens.MuiButton.activeScale + ')',
          },
          '&:focus-visible': focusRing(theme),
        }),
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
          '&:focus-visible': focusRing(theme),
        }),
      },
    },
    MuiIconButton: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          borderRadius: `${componentTokens.MuiButton.borderRadius}px`,
          transition:
            'background-color ' +
            effectTokens.motion.duration +
            ' ' +
            effectTokens.motion.easing +
            ', transform ' +
            effectTokens.motion.duration +
            ' ' +
            effectTokens.motion.easing,
          '&:active': {
            transform: 'scale(' + componentTokens.MuiButton.activeScale + ')',
          },
          '&:focus-visible': focusRing(theme),
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
    MuiListItemButton: {
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          transition:
            'background-color ' +
            effectTokens.motion.duration +
            ' ' +
            effectTokens.motion.easing +
            ', transform ' +
            effectTokens.motion.duration +
            ' ' +
            effectTokens.motion.easing,
          '&:active': {
            transform: 'scale(' + componentTokens.MuiListItemButton.activeScale + ')',
          },
          '&:focus-visible': {
            ...focusRing(theme),
            outlineOffset: '-' + effectTokens.focus.offset,
          },
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
    MuiDataGrid: {
      defaultProps: { density: 'compact' },
      styleOverrides: {
        root: ({ theme }: { theme: Theme }) => ({
          border: 'none',
          '& .MuiDataGrid-columnHeaders': {
            backgroundColor: theme.vars.palette.action.hover,
          },
        }),
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
