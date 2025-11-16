/**   
 * ESLint config for Bridge Frontend (Design Spec v7.0 + MUI v7+ + Strict Tokenization)  
 *   
 * Enforces:  
 *  - No deep imports from @mui packages (ESM only)  
 *  - No legacy v5/v6 APIs (theme.palette.mode, theme.applyStyles, direct colorSchemes access)  
 *  - Use slots + slotProps instead of deprecated props  
 *  - No inline hex colors outside tokens.json  
 *  - Use theme.vars for styling, not stale theme.palette values  
 *  - Enforce Tier 1 token usage (spacing, shadows, borders, backgrounds)  
 *  - All custom semantic colors created with augmentColor()  
 *  - Accessibility rules (WCAG 2.2 AA) — errors block CI  
 *  - React + TypeScript best practices  
 *  - Enforce MUI v7 Grid `size={{}}` API instead of legacy `item` / `xs` / `md`  
 */  
module.exports = {  
  root: true,  
  ignorePatterns: [  
    'node_modules/',  
    'dist/',  
  ],  
  overrides: [  
    {  
      files: ['src/**/*.{ts,tsx}'],  
      parser: '@typescript-eslint/parser',  
      parserOptions: {  
        tsconfigRootDir: __dirname,  
        project: ['./tsconfig.json'],  
        ecmaVersion: 2021,  
        sourceType: 'module',  
      },  
      plugins: [  
        '@typescript-eslint',  
        'react',  
        'jsx-a11y',  
        'import',  
        'eslint-plugin-regexp'  
      ],  
      extends: [  
        'eslint:recommended',  
        'plugin:@typescript-eslint/recommended',  
        'plugin:react/recommended',  
        'plugin:jsx-a11y/recommended',  
        'plugin:import/recommended',  
        'plugin:import/typescript'  
      ],  
      rules: {  
        // === MUI v7+ ESM Import Rules ===  
        'no-restricted-imports': [  
          'error',  
          {  
            patterns: [  
              '@mui/*/*/*',  
              '@mui/*/*',  
              '@mui/icons-material/esm/*',  
              '@mui/icons-material/*/*',  
              '@mui/material/Hidden'  
            ],  
            paths: [  
              {  
                name: '@mui/material',  
                importNames: ['GridLegacy'],  
                message: 'GridLegacy is only for staged migration — prefer Grid v7 default.',  
              }  
            ]  
          }  
        ],  
  
        // === v7+ Component API Enforcement ===  
        'no-restricted-syntax': [  
          'error',  
          {  
            selector: 'JSXAttribute[name.name="TransitionComponent"]',  
            message: 'Use slots={{ transition: ... }} instead of TransitionComponent in MUI v7+.',  
          },  
          {  
            selector: 'JSXAttribute[name.name="TransitionProps"]',  
            message: 'Use slotProps={{ transition: {...} }} instead of TransitionProps in MUI v7+.',  
          },  
          {  
            selector: 'JSXIdentifier[name="Hidden"]',  
            message: 'Hidden component is deprecated — use sx responsive object or useMediaQuery.',  
          },  
          {  
            selector: 'JSXAttribute[name.name="size"][value.value="normal"]',  
            message: 'Use size="medium" in MUI v7+.',  
          },  
          {  
            selector: 'JSXAttribute[name.name="onBackgroundClick"]',  
            message: 'Use onClose instead of onBackgroundClick in MUI v7+.',  
          },  
          {  
            selector: 'JSXAttribute[name.name="slotProps"]',  
            message: 'Ensure slotProps usage is type-safe and matches the new component token system.',  
          },  
          // 🚫 NEW — Block legacy Grid item/Breakpoint props  
          {  
            selector: 'JSXAttribute[name.name="item"]',  
            message: 'MUI v7 Grid items no longer use `item` — use size={{ xs: ..., md: ... }} instead.',  
          },  
          {  
            selector: 'JSXAttribute[name.name=/^(xs|sm|md|lg|xl)$/]',  
            message: 'MUI v7 Grid breakpoints must use size={{ xs: ..., md: ... }} object syntax.',  
          }  
        ],  
  
        // === Inline Hex Color / Tokenization Enforcement ===  
        'regexp/match': [  
          'error',  
          {  
            pattern: '#[0-9a-fA-F]{3,8}',  
            message: 'Avoid inline hex colors — use theme.vars palette tokens from tokens.json.',  
          },  
          {  
            pattern: 'alpha\\$theme\\.palette\\.',  
            message: 'Use CSS color-mix() or theme.vars.palette.* selectors in MUI v7+.',  
          },  
          {  
            pattern: 'theme\\.spacing\\([^)]',  
            message: 'Do not use theme.spacing() directly — use Tier 1 spacing tokens from theme.layout.* (Spec 6.1).',  
          },  
          {  
            pattern: 'theme\\.shadows\\$[0-9]+',  
            message: 'Use Tier 1 shadow tokens from theme.effects.shadow.*, not numeric theme.shadows index.',  
          },  
          {  
            pattern: 'border[^:]*:\\s*[^;]*#[0-9a-fA-F]{3,8}',  
            message: 'Borders must use outline tokens from theme.vars.palette.outline.* or componentTokens.*.',  
          },  
          {  
            pattern: 'background(Color)?:\\s*#[0-9a-fA-F]{3,8}',  
            message: 'Background colors must use Tier 1 or Tier 2 palette tokens via theme.vars.palette.*.',  
          },  
          {  
            pattern: 'theme\\.palette\\.mode',  
            message: 'Do not use theme.palette.mode — use [theme.getColorSchemeSelector(mode)] selectors instead.',  
          },  
          {  
            pattern: 'theme\\.colorSchemes',  
            message: 'Do not access theme.colorSchemes directly — use theme.vars.palette.* or selectors.',  
          },  
          // === New v7.0 Enforcements ===  
          {  
            pattern: 'theme\\.applyStyles',  
            message: 'Do not use theme.applyStyles() — use [theme.getColorSchemeSelector(mode)] selectors instead (Spec 11.3).',  
          },  
          {  
            pattern: 'palette\\.(accent|calm|warmNeutral)\\s*:\\s*{\\s*main:',  
            message: 'Custom semantic colors must be created via theme.palette.augmentColor() (Spec 6.2).',  
          },  
          {  
            pattern: 'theme\\.colorSchemes\\.(light|dark)',  
            message: 'Do not access theme.colorSchemes.light/dark directly — use theme.vars.palette.* or selectors.',  
          },  
          {  
            pattern: 'createTheme\\([^)]*\\(.*tokens\\.',  
            message: 'All token aliases must be resolved at build time — import static values from pre-processed tokens.json (Spec VI.B).',  
          },  
          {  
            pattern: 'theme\\.palette\\.',  
            message: 'Do not use theme.palette.* in sx — use theme.vars.palette.* for mode-aware CSS variables (Spec 11.3).',  
          }  
        ],  
  
        // === Theming Best Practices ===  
        'no-restricted-properties': [  
          'error',  
          {  
            object: 'theme',  
            property: 'palette',  
            message: 'Use theme.vars.palette.* for live CSS variable values in MUI v7+.',  
          },  
          {  
            object: 'theme',  
            property: 'colorSchemes',  
            message: 'Do not access theme.colorSchemes directly — use theme.vars.palette.* or selectors.',  
          },  
        ],  
  
        // === Accessibility (WCAG 2.2 AA) ===  
        'jsx-a11y/color-contrast': ['error', { standard: 'WCAG2AA', ignoreAlpha: true }],  
        'jsx-a11y/alt-text': 'warn',  
        'jsx-a11y/anchor-is-valid': 'warn',  
        'jsx-a11y/no-static-element-interactions': 'warn',  
        'jsx-a11y/no-noninteractive-element-interactions': 'warn',  
        'jsx-a11y/mouse-events-have-key-events': 'warn',  
        'jsx-a11y/aria-role': 'warn',  
        'jsx-a11y/aria-props': 'warn',  
        'jsx-a11y/no-redundant-roles': 'warn',  
        'jsx-a11y/role-has-required-aria-props': 'warn',  
  
        // === React Best Practices ===  
        'react/prop-types': 'off',  
        'react/react-in-jsx-scope': 'off',  
        'react/jsx-uses-react': 'off',  
        'react/jsx-uses-vars': 'error',  
  
        // === Import Hygiene ===  
        'import/no-unresolved': 'error',  
        'import/order': [  
          'warn',  
          {  
            groups: ['builtin', 'external', 'internal', ['parent', 'sibling', 'index']],  
            alphabetize: { order: 'asc', caseInsensitive: true }  
          }  
        ]  
      },  
      settings: {  
        react: {  
          version: 'detect'  
        }  
      }  
    }  
  ]  
};  