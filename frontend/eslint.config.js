// eslint.config.js  
  
import js from "@eslint/js";  
import globals from "globals";  
import tsParser from "@typescript-eslint/parser";  
import tsPlugin from "@typescript-eslint/eslint-plugin";  
import reactPlugin from "eslint-plugin-react";  
import jsxA11yPlugin from "eslint-plugin-jsx-a11y";  
import importPlugin from "eslint-plugin-import";  
  
// Make sure you've installed:  
// npm install --save-dev eslint-import-resolver-typescript  
  
export default [  
  {  
    // Ignore build artifacts, dependencies, and generated sources
    ignores: [
      "node_modules/",
      "dist/",
      "build/",
      "src/theme/generated/**",
      "src/types/backend.ts",
      "src/types/backend-wss.ts",
    ],
  },  
  {  
    files: ["src/**/*.{ts,tsx}"],  
  
    languageOptions: {  
      parser: tsParser,  
      parserOptions: {  
        tsconfigRootDir: import.meta.dirname,  
        project: ["./tsconfig.json"],  
        ecmaVersion: 2021,  
        sourceType: "module",  
      },  
      globals: {  
        ...globals.browser,  
        chrome: "readonly",  
        NodeJS: "readonly",  
        React: "readonly",  
      },  
    },  
  
    plugins: {  
      "@typescript-eslint": tsPlugin,  
      react: reactPlugin,  
      "jsx-a11y": jsxA11yPlugin,  
      import: importPlugin,  
    },  
  
    settings: {  
      react: { version: "detect" },  
      "import/resolver": {  
        typescript: {  
          project: "./tsconfig.json",  
          alwaysTryTypes: true, // also resolve @types packages  
        },  
        node: {  
          extensions: [".js", ".jsx", ".ts", ".tsx"],  
        },  
      },  
    },  
  
    rules: {  
      // Base recommended rules  
      ...js.configs.recommended.rules,  
      ...tsPlugin.configs.recommended.rules,  
      ...reactPlugin.configs.recommended.rules,  
      ...jsxA11yPlugin.configs.recommended.rules,  
      ...importPlugin.configs.recommended.rules,  
      ...importPlugin.configs.typescript.rules,  
  
      // Style consistency
      "quotes": ["error", "single", { avoidEscape: true }],
      "jsx-quotes": ["error", "prefer-double"],

      // MUI v9 and design-system guardrails
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "@mui/icons-material",
              message:
                "Import icons by path (e.g. @mui/icons-material/Delete) for tree-shaking.",
            },
            {
              name: "@mui/styles",
              message:
                "The JSS styling engine is removed in MUI v9. Use the sx prop or styled().",
            },
            {
              name: "@mui/material/styles",
              importNames: ["makeStyles", "withStyles", "createMuiTheme"],
              message:
                "Legacy styling API. Use the sx prop, styled(), or createTheme.",
            },
          ],
          patterns: [
            {
              group: ["@material-ui", "@material-ui/*"],
              message: "Legacy MUI v4 packages. Use @mui/* v9.",
            },
          ],
        },
      ],
      "no-restricted-properties": "warn",
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "MemberExpression[object.type='Identifier'][object.name='theme'][property.name='palette']",
          message:
            "Use theme.vars.palette.* so color-scheme switching works; theme.palette.* bypasses the CSS variables.",
        },
      ],
      "import/no-unresolved": "warn",  
      "@typescript-eslint/no-explicit-any": "warn",  
      "no-undef": "warn",  
  
      // Ensure ESLint doesn't complain about missing extensions for TS/JS imports  
      "import/extensions": [  
        "warn",  
        "ignorePackages",  
        {  
          ts: "never",  
          tsx: "never",  
          js: "never",  
          jsx: "never",  
        },  
      ],  
  
      // Accessibility  
      "jsx-a11y/alt-text": "warn",  
      "jsx-a11y/anchor-is-valid": "warn",  
      "jsx-a11y/no-static-element-interactions": "warn",  
      "jsx-a11y/no-noninteractive-element-interactions": "warn",  
      "jsx-a11y/mouse-events-have-key-events": "warn",  
      "jsx-a11y/aria-role": "warn",  
      "jsx-a11y/aria-props": "warn",  
      "jsx-a11y/no-redundant-roles": "warn",  
      "jsx-a11y/role-has-required-aria-props": "warn",  
  
      // React  
      "react/prop-types": "off",  
      "react/react-in-jsx-scope": "off",  
      "react/jsx-uses-react": "off",  
      "react/jsx-uses-vars": "warn",  
  
      // Import order  
      "import/order": [  
        "warn",  
        {  
          groups: [  
            "builtin",  
            "external",  
            "internal",  
            ["parent", "sibling", "index"],  
          ],  
          alphabetize: { order: "asc", caseInsensitive: true },  
        },  
      ],  
    },  
  },  
];  