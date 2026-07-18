import '@mui/material/Button';
import '@mui/material/Chip';
import '@mui/material/styles';
import type { PaletteColor, PaletteColorOptions, Theme as MuiTheme } from '@mui/material/styles';

import type { brandPalette, brandTypography } from '../theme/generated/tokens';

type BridgeEffectStyles = Record<string, string | number>;

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
