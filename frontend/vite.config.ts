import { defineConfig } from 'vite';  
import react from '@vitejs/plugin-react-swc';  
import { aliasEntries } from './scripts/aliases.config.js';  
import { fileURLToPath } from 'url';  
import path, { resolve } from 'path';  
  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = path.dirname(__filename);  
  
export default defineConfig({  
  plugins: [  
    react({  
      jsxImportSource: '@emotion/react',  
      plugins: [  
        ['@swc/plugin-emotion', { sourceMap: true, autoLabel: 'dev-only' }]  
      ]  
    })  
  ],  
  resolve: {  
    alias: aliasEntries  
  },  
  base: process.env.VITE_BASE_PATH || './',  
  build: {  
    outDir: resolve(__dirname, '../app/static/dist'),  
    emptyOutDir: true,  
    manifest: 'manifest.json',  
    rollupOptions: {  
      input: resolve(__dirname, 'index.html'),  
      output: {  
        manualChunks: {  
          react: ['react', 'react-dom'],  
          mui: [  
            '@mui/material',  
            '@mui/icons-material',  
            '@emotion/react',  
            '@emotion/styled'  
          ]  
        }  
      }  
    },  
    chunkSizeWarningLimit: 1000  
  },  
  server: {  
    port: 5173,  
    open: true,  
    proxy: {  
      '/api': { target: 'http://localhost:8000', changeOrigin: true },  
      '/openapi.json': { target: 'http://localhost:8000', changeOrigin: true },  
      '/api/v1/schemas/wss': { target: 'http://localhost:8000', changeOrigin: true },  
      '/ws': { target: 'ws://localhost:8000', ws: true, changeOrigin: true }  
    }  
  },  
  ssr: {  
    noExternal: process.env.NODE_ENV === 'production' ? [] : ['@mui/material']  
  }  
});  