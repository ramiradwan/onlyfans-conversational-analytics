// src/theme.ts
import { createTheme, alpha } from '@mui/material/styles';

export const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: '#2563EB', light: '#3B82F6', dark: '#1E40AF' },
    success: { main: '#16A34A', light: '#4ADE80', dark: '#15803D' },
    warning: { main: '#FACC15', light: '#FDE047', dark: '#CA8A04' },
    error: { main: '#DC2626', light: '#F87171', dark: '#B91C1C' },
    info: { main: '#3B82F6', light: '#60A5FA', dark: '#1D4ED8' },
    background: { default: '#F9FAFB', paper: '#FFFFFF' },
    text: { primary: '#111827', secondary: '#6B7280' },
    divider: '#E5E7EB',
    action: {
      hover: alpha('#000', 0.04),
      selected: alpha('#2563EB', 0.08),
      disabledOpacity: 0.38,
      focus: alpha('#2563EB', 0.2),
    },
  },

  shape: { borderRadius: 12 },
  spacing: 8,

  typography: {
    fontFamily: '"Inter", "Roboto", "Helvetica", "Arial", sans-serif',
    fontWeightRegular: 400,
    fontWeightMedium: 500,
    fontWeightBold: 700,
    h1: { fontSize: '2.5rem', fontWeight: 700, lineHeight: 1.2 },
    h2: { fontSize: '2rem', fontWeight: 600, lineHeight: 1.3 },
    h3: { fontSize: '1.75rem', fontWeight: 600 },
    h6: { fontSize: '1.25rem', fontWeight: 500 },
    body1: { fontSize: '1rem', lineHeight: 1.6 },
    body2: { fontSize: '0.875rem', color: '#6B7280', lineHeight: 1.5 },
    caption: { fontSize: '0.75rem', color: '#6B7280' },
    button: { textTransform: 'none', fontWeight: 500 },
  },

  components: {
    MuiCssBaseline: {
      styleOverrides: {
        html: {
          scrollBehavior: 'smooth',
        },
        body: {
          backgroundColor: '#F9FAFB',
          color: '#111827',
          fontFeatureSettings: '"liga","kern"',
          WebkitFontSmoothing: 'antialiased',
          MozOsxFontSmoothing: 'grayscale',
        },
        '::-webkit-scrollbar': { width: 8 },
        '::-webkit-scrollbar-thumb': {
          background: 'rgba(0,0,0,0.2)',
          borderRadius: 4,
        },
        '::-webkit-scrollbar-thumb:hover': {
          background: 'rgba(0,0,0,0.3)',
        },
      },
    },

    MuiAppBar: {
      styleOverrides: {
        root: {
          background: 'linear-gradient(90deg, #2563EB 0%, #1E40AF 100%)',
          boxShadow: 'none',
          borderBottom: '1px solid rgba(255,255,255,0.1)',
          backdropFilter: 'blur(8px)',
        },
      },
    },

    MuiDrawer: {
      styleOverrides: {
        paper: {
          backgroundColor: '#FFFFFF',
          boxShadow: '0 0 20px rgba(0,0,0,0.05)',
        },
      },
    },

    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: {
          textTransform: 'none',
          borderRadius: 8,
          fontWeight: 500,
          transition: 'background-color 0.2s ease, box-shadow 0.2s ease',
          '&:hover': {
            boxShadow: '0 2px 8px rgba(37, 99, 235, 0.25)',
          },
        },
        containedPrimary: {
          color: '#fff',
        },
      },
    },

    MuiPaper: {
      styleOverrides: {
        rounded: { borderRadius: 12 },
        elevation1: { boxShadow: '0 1px 3px rgba(0,0,0,0.08)' },
      },
    },

    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: 12,
          boxShadow: '0 1px 4px rgba(0,0,0,0.08)',
          transition: 'box-shadow 0.25s ease, transform 0.15s ease',
          '&:hover': {
            boxShadow: '0 4px 14px rgba(0,0,0,0.12)',
            transform: 'translateY(-2px)',
          },
        },
      },
    },

    MuiIconButton: {
      styleOverrides: {
        root: {
          borderRadius: 8,
          '&:focus-visible': {
            outline: `2px solid #2563EB`,
            outlineOffset: 2,
          },
        },
      },
    },

    MuiTextField: {
      styleOverrides: {
        root: {
          '& .MuiOutlinedInput-root': {
            borderRadius: 8,
            transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
            '&.Mui-focused fieldset': {
              borderColor: '#2563EB',
              boxShadow: '0 0 0 3px rgba(37,99,235,0.1)',
            },
          },
        },
      },
    },
  },
});
