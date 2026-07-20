import '@mui/material/Button';
import '@mui/material/Chip';
import '@mui/material/styles';
import type { PaletteColor, PaletteColorOptions, Theme as MuiTheme } from '@mui/material/styles';
import type { CSSProperties } from '@mui/system';

import type { brandPalette, brandTypography } from '../theme/generated/tokens';

type BridgeEffectStyles = CSSProperties & {
  '&::before'?: CSSProperties;
};

declare module '@mui/material/styles' {
  interface Theme {
    brandPalette: typeof brandPalette;
    brandTypography: typeof brandTypography;
    effects: {
      cardBorder(theme: MuiTheme): BridgeEffectStyles;
      chartFrame(theme: MuiTheme): BridgeEffectStyles;
      glassmorphism(theme: MuiTheme): BridgeEffectStyles;
      headerBorder(theme: MuiTheme): BridgeEffectStyles;
      sideBorder(theme: MuiTheme): BridgeEffectStyles;
    };
  }

  interface ThemeOptions {
    brandPalette?: typeof brandPalette;
    brandTypography?: typeof brandTypography;
    effects?: {
      cardBorder?(theme: MuiTheme): BridgeEffectStyles;
      chartFrame?(theme: MuiTheme): BridgeEffectStyles;
      glassmorphism?(theme: MuiTheme): BridgeEffectStyles;
      headerBorder?(theme: MuiTheme): BridgeEffectStyles;
      sideBorder?(theme: MuiTheme): BridgeEffectStyles;
    };
  }

  interface Palette {
    accent: PaletteColor;
    calm: PaletteColor;
    placeholder: string;
    surface: {
      subtle: string;
      glass: string;
      chartInsetShadow: string;
      elevation: string;
      rim: string;
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

declare module '@mui/material/Chip' {
  interface ChipPropsColorOverrides {
    accent: true;
    calm: true;
  }
}

declare module '@mui/x-data-grid/themeAugmentation' {
  interface Components {
    MuiDataGrid?: {
      defaultProps?: Record<string, unknown>;
      styleOverrides?: {
        root?:
          | React.CSSProperties
          | ((props: { theme: MuiTheme }) => React.CSSProperties);
        [slot: string]:
          | React.CSSProperties
          | ((props: { theme: MuiTheme }) => React.CSSProperties)
          | undefined;
      };
    };
  }
}
