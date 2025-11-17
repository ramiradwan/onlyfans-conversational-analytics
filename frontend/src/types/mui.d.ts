import '@mui/material/styles';  
import type { Theme as MuiTheme } from '@mui/material/styles';  
import type { brandPaletteTokens } from '@theme/brandTokens';  
  
// ---- Core MUI Theme Augmentation ----  
declare module '@mui/material/styles' {  
  interface Theme {  
    brandPalette: typeof brandPaletteTokens;  
    effects: {  
      cardBorder: (theme: Theme) => Record<string, unknown>;  
      chartFrame: (theme: Theme) => Record<string, unknown>;  
      headerBorder: (theme: Theme) => Record<string, unknown>;  
      sideBorder: (theme: Theme) => Record<string, unknown>;  
    };  
    layout: {  
      pagePadding: number;  
      sectionSpacing: number;  
    };  
  }  
  
  interface ThemeOptions {  
    brandPalette?: typeof brandPaletteTokens;  
    effects?: {  
      cardBorder?: (theme: Theme) => Record<string, unknown>;  
      chartFrame?: (theme: Theme) => Record<string, unknown>;  
      headerBorder?: (theme: Theme) => Record<string, unknown>;  
      sideBorder?: (theme: Theme) => Record<string, unknown>;  
    };  
    layout?: {  
      pagePadding?: number;  
      sectionSpacing?: number;  
    };  
  }  
  
  interface Palette {  
    brand: {  
      groundedTechPrimary: string;  
      groundedTechWarmNeutral: string;  
      optimisticAccentPrimary: string;  
      optimisticAccentMuted: string;  
      calmClearPrimary: string;  
      calmClearEtherealBlue: string;  
    };  
    placeholder: string;  
    chart: {  
      sentiment: string;  
      volume: string;  
      neutral: string;  
    };  
  }  
  
  interface PaletteOptions {  
    brand?: {  
      groundedTechPrimary?: string;  
      groundedTechWarmNeutral?: string;  
      optimisticAccentPrimary?: string;  
      optimisticAccentMuted?: string;  
      calmClearPrimary?: string;  
      calmClearEtherealBlue?: string;  
    };  
    placeholder?: string;  
    chart?: {  
      sentiment?: string;  
      volume?: string;  
      neutral?: string;  
    };  
  }  
}  
  
// ---- DataGrid augmentation for theme.components ----  
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