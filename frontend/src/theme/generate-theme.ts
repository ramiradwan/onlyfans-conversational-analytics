import fs from 'fs';  
import path from 'path';  
import { fileURLToPath } from 'url';  
  
// ESM-safe __dirname  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = path.dirname(__filename);  
  
const tokensPath = path.join(__dirname, 'tokens.json');  
const tokens = JSON.parse(fs.readFileSync(tokensPath, 'utf8'));  
  
// Write helper  
function write(relPath: string, content: string) {  
  const full = path.join(__dirname, relPath);  
  fs.mkdirSync(path.dirname(full), { recursive: true });  
  fs.writeFileSync(full, content, 'utf8');  
}  
  
// Header for generated files  
function header(name: string) {  
  return `// AUTO-GENERATED FILE: ${name}\n// DO NOT EDIT DIRECTLY — edit tokens.json instead.\n\n`;  
}  
  
// Build palette from tokens.json  
function makePalette(mode: 'light' | 'dark') {  
  const bp = tokens.brandPalette;  
  const sp = tokens.semanticPalette[mode];  
  
  return {  
    contrastThreshold: sp.contrastThreshold,  
    primary: {  
      main: mode === 'light' ? bp.groundedTech.primary : bp.groundedTech.primaryDark,  
      contrastText: sp.primaryContrastText,  
    },  
    secondary: {  
      main: mode === 'light' ? bp.optimisticAccent.primary : bp.optimisticAccent.muted,  
      contrastText: sp.secondaryContrastText,  
    },  
    background: {  
      default: mode === 'light' ? bp.calmClear.lightBg : bp.calmClear.darkBg,  
      paper: mode === 'light' ? bp.calmClear.lightPaper : bp.calmClear.darkPaper,  
    },  
    text: { primary: sp.textPrimary, secondary: sp.textSecondary },  
    brand: {  
      groundedTechPrimary: mode === 'light' ? bp.groundedTech.primary : bp.groundedTech.primaryDark,  
      groundedTechWarmNeutral: bp.groundedTech.warmNeutral,  
      optimisticAccentPrimary: bp.optimisticAccent.primary,  
      optimisticAccentMuted: bp.optimisticAccent.muted,  
      calmClearPrimary: bp.calmClear.primary,  
      calmClearEtherealBlue: bp.calmClear.etherealBlue,  
    },  
    action: {  
      hover: sp.actionHover,  
      selected: sp.actionSelected,  
      disabled: sp.actionDisabled,  
    },  
    divider: sp.divider,  
    placeholder: sp.placeholder,  
    chart: {  
      sentiment: mode === 'light' ? bp.groundedTech.primary : bp.groundedTech.primaryDark,  
      volume: bp.optimisticAccent.primary,  
      neutral: bp.calmClear.etherealBlue,  
    },  
  };  
}  
  
// --- 1) tokens.ts ---  
write(  
  'generated/tokens.ts',  
  header('tokens.ts') +  
    `export const brandPalette = ${JSON.stringify(tokens.brandPalette, null, 2)} as const;\n\n` +  
    `export const typography = ${JSON.stringify(tokens.typography, null, 2)} as const;\n\n` +  
    `export const layout = ${JSON.stringify(tokens.layout, null, 2)} as const;\n\n` +  
    `export const skeletonTokens = ${JSON.stringify(tokens.skeleton, null, 2)} as const;\n\n` +  
    `export const effectsTokens = ${JSON.stringify(tokens.effects, null, 2)} as const;\n\n` +  
    `export const paletteLight = ${JSON.stringify(makePalette('light'), null, 2)} as const;\n\n` +  
    `export const paletteDark = ${JSON.stringify(makePalette('dark'), null, 2)} as const;\n`  
);  
  
// --- 2) theme.ts ---  
write(  
  'generated/theme.ts',  
  header('theme.ts') +  
    `import { createTheme, Theme } from '@mui/material/styles';\n` +  
    `import type {} from '@mui/material/themeCssVarsAugmentation';\n` +  
    `import type {} from '@mui/x-data-grid/themeAugmentation';\n` +  
    `import { brandPalette, typography, layout, paletteLight, paletteDark } from './tokens';\n\n` +  
    `export const theme = createTheme({\n` +  
    `  brandPalette,\n` +  
    `  cssVariables: { cssVarPrefix: 'bridge', colorSchemeSelector: 'class' },\n` +  
    `  colorSchemes: { light: { palette: paletteLight }, dark: { palette: paletteDark } },\n` +  
    `  typography,\n` +  
    `  effects: {\n` +  
    `    cardBorder: (theme: Theme) => ({ border: \`1px solid \${theme.vars.palette.divider}\`, borderRadius: 8 }),\n` +  
    `    chartFrame: (theme: Theme) => ({\n` +  
    `      border: \`1px solid \${theme.vars.palette.divider}\`,\n` +  
    `      borderRadius: 4,\n` +  
    `      boxShadow: theme.palette.mode === 'dark'\n` +  
    `        ? 'inset 0 0 3px rgba(255,255,255,0.08)'\n` +  
    `        : 'inset 0 0 3px rgba(0,0,0,0.05)'\n` +  
    `    }),\n` +  
    `    headerBorder: (theme: Theme) => ({ borderBottom: \`1px solid \${theme.vars.palette.divider}\` }),\n` +  
    `    sideBorder: (theme: Theme) => ({ borderRight: \`1px solid \${theme.vars.palette.divider}\` })\n` +  
    `  },\n` +  
    `  layout,\n` +  
    `  components: {\n` +  
    `    MuiButton: {\n` +  
    `      styleOverrides: {\n` +  
    `        root: ({ theme }: { theme: Theme }) => [\n` +  
    `          { borderRadius: 8, boxShadow: 'none', '&:hover': { boxShadow: 'none' } },\n` +  
    `          theme.applyStyles('dark', {\n` +  
    `            backgroundColor: theme.vars.palette.secondary.main,\n` +  
    `            '&:hover': { backgroundColor: theme.vars.palette.secondary.dark }\n` +  
    `          })\n` +  
    `        ]\n` +  
    `      }\n` +  
    `    },\n` +  
    `    MuiCard: {\n` +  
    `      styleOverrides: {\n` +  
    `        root: ({ theme }: { theme: Theme }) => ({\n` +  
    `          boxShadow: 'none',\n` +  
    `          border: \`1px solid \${theme.vars.palette.divider}\`\n` +  
    `        })\n` +  
    `      }\n` +  
    `    },\n` +  
    `    MuiPaper: {\n` +  
    `      styleOverrides: {\n` +  
    `        outlined: ({ theme }: { theme: Theme }) => ({\n` +  
    `          backgroundColor: theme.vars.palette.background.paper,\n` +  
    `          border: \`1px solid \${theme.vars.palette.divider}\`,\n` +  
    `          boxShadow: 'none'\n` +  
    `        })\n` +  
    `      }\n` +  
    `    },\n` +  
    `    MuiDataGrid: {\n` +  
    `      styleOverrides: {\n` +  
    `        root: ({ theme }: { theme: Theme }) => ({\n` +  
    `          border: 'none',\n` +  
    `          '& .MuiDataGrid-columnHeaders': {\n` +  
    `            backgroundColor: theme.vars.palette.action.hover\n` +  
    `          }\n` +  
    `        })\n` +  
    `      }\n` +  
    `    }\n` +  
    `  }\n` +  
    `});\n`  
);  
  
// --- 3) index.ts (barrel) ---  
write(  
  'index.ts',  
  header('index.ts') +  
    `export * from './generated/theme';\n` +  
    `export * from './generated/tokens';\n`  
);  
  
console.log('✅ Theme tokens & public API generated from tokens.json');  