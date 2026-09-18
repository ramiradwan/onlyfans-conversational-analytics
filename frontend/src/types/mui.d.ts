import '@mui/material/Button';
import '@mui/material/Chip';
import '@mui/material/styles';
import '@mui/material/Typography';
import type { PaletteColor, PaletteColorOptions, Theme as MuiTheme } from '@mui/material/styles';
import type { CSSProperties } from '@mui/system';
import type { CSSProperties as ReactCSSProperties } from 'react';

import type { brandPalette, brandTypography } from '../theme/generated/tokens';

type BridgeEffectStyles = CSSProperties & {
  '&::before'?: CSSProperties;
};

declare module '@mui/material/styles' {
  interface TypeAction {
    selectedForeground: string;
  }

  interface TypeText {
    muted: string;
  }

  interface Theme {
    brandPalette: typeof brandPalette;
    brandTypography: typeof brandTypography;
    effects: {
      ambientGlow(theme: MuiTheme): BridgeEffectStyles;
      cardBorder(theme: MuiTheme): BridgeEffectStyles;
      chartFrame(theme: MuiTheme): BridgeEffectStyles;
      glassmorphism(theme: MuiTheme): BridgeEffectStyles;
      headerBorder(theme: MuiTheme): BridgeEffectStyles;
      overlay(theme: MuiTheme): BridgeEffectStyles;
      sideBorder(theme: MuiTheme): BridgeEffectStyles;
    };
  }

  interface ThemeOptions {
    brandPalette?: typeof brandPalette;
    brandTypography?: typeof brandTypography;
    effects?: {
      ambientGlow?(theme: MuiTheme): BridgeEffectStyles;
      cardBorder?(theme: MuiTheme): BridgeEffectStyles;
      chartFrame?(theme: MuiTheme): BridgeEffectStyles;
      glassmorphism?(theme: MuiTheme): BridgeEffectStyles;
      headerBorder?(theme: MuiTheme): BridgeEffectStyles;
      overlay?(theme: MuiTheme): BridgeEffectStyles;
      sideBorder?(theme: MuiTheme): BridgeEffectStyles;
    };
  }

  interface TypographyVariants {
    insight: ReactCSSProperties;
    metricUnit: ReactCSSProperties;
    numericCaption: ReactCSSProperties;
    numericBody: ReactCSSProperties;
    detailLabel: ReactCSSProperties;
    tableHeading: ReactCSSProperties;
    passkeyTitle: ReactCSSProperties;
    kpi: ReactCSSProperties;
    metric: ReactCSSProperties;
  }

  interface TypographyVariantsOptions {
    insight?: ReactCSSProperties;
    metricUnit?: ReactCSSProperties;
    numericCaption?: ReactCSSProperties;
    numericBody?: ReactCSSProperties;
    detailLabel?: ReactCSSProperties;
    tableHeading?: ReactCSSProperties;
    passkeyTitle?: ReactCSSProperties;
    kpi?: ReactCSSProperties;
    metric?: ReactCSSProperties;
  }

  interface Palette {
    measurement: PaletteColor;
    brand: PaletteColor;
    sentiment: Record<'positive' | 'negative' | 'neutral' | 'unknown', string>;
    accent: PaletteColor;
    calm: PaletteColor;
    placeholder: string;
    surface: {
      avatar: { fill: string; text: string };
      trust: { fill: string; ink: string; border: string };
      segment: { track: string; selected: string; elevation: string };
      tooltip: { fill: string; text: string };
      subtle: string;
      glass: string;
      elevation: string;
      overlay: string;
      rim: string;
      glow: string;
      dominant: { border: string; elevation: string };
      error: string;
      metric: Record<'measurement' | 'sentiment' | 'opportunity' | 'connection', { fill: string; border: string }>;
    };
    communication: {
      incomingSurface: string;
      incomingBorder: string;
      outgoingSurface: string;
      outgoingBorder: string;
    };
    chart: {
      sentiment: string;
      area: string;
      baseline: string;
      volume: string;
      neutral: string;
      positive: string;
      negative: string;
      unknown: string;
      categorical1: string;
      categorical2: string;
      categorical3: string;
      categorical4: string;
      categorical5: string;
      categorical6: string;
      categorical7: string;
      categorical8: string;
      opportunity: string;
      grid: string;
    };
  }

  interface PaletteOptions {
    measurement?: PaletteColorOptions;
    brand?: PaletteColorOptions;
    sentiment?: Palette['sentiment'];
    accent?: PaletteColorOptions;
    calm?: PaletteColorOptions;
    placeholder?: string;
    surface?: Partial<Palette['surface']>;
    communication?: Partial<Palette['communication']>;
    chart?: Partial<Palette['chart']>;
  }
}

declare module '@mui/material/Button' {
  interface ButtonPropsColorOverrides {
    accent: true;
    calm: true;
  }
}

declare module '@mui/material/Typography' {
  interface TypographyPropsVariantOverrides {
    insight: true;
    metricUnit: true;
    numericCaption: true;
    numericBody: true;
    detailLabel: true;
    tableHeading: true;
    passkeyTitle: true;
    kpi: true;
    metric: true;
  }
}

declare module '@mui/material/Chip' {
  interface ChipPropsColorOverrides {
    accent: true;
    calm: true;
  }
}
