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
    kpi: ReactCSSProperties;
    metric: ReactCSSProperties;
  }

  interface TypographyVariantsOptions {
    kpi?: ReactCSSProperties;
    metric?: ReactCSSProperties;
  }

  interface Palette {
    measurement: PaletteColor;
    sentiment: Record<'positive' | 'negative' | 'neutral' | 'unknown', string>;
    accent: PaletteColor;
    calm: PaletteColor;
    placeholder: string;
    surface: {
      subtle: string;
      glass: string;
      elevation: string;
      overlay: string;
      rim: string;
      glow: string;
    };
    communication: {
      incomingSurface: string;
      incomingBorder: string;
      outgoingSurface: string;
      outgoingBorder: string;
    };
    chart: {
      sentiment: string;
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
