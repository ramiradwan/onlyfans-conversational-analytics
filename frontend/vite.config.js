import { defineConfig } from 'vite';  
import react from '@vitejs/plugin-react';  
import { resolve, dirname } from 'path';  
import { fileURLToPath } from 'url';  
  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = dirname(__filename);  
  
export default defineConfig({  
  plugins: [  
    react({  
      babel: {  
        plugins: ['@emotion/babel-plugin'], // ✅ recommended for MUI + Emotion  
      },  
    }),  
  ],  
  resolve: {  
    extensions: ['.js', '.jsx', '.ts', '.tsx'],  
    alias: {  
      '@types': resolve(__dirname, 'src/types'),  
      '@components': resolve(__dirname, 'src/components'),  
      '@': resolve(__dirname, 'src'),  
    },  
  },  
  base: './',  
  build: {  
    outDir: resolve(__dirname, '../app/static/dist'),  
    emptyOutDir: true,  
    manifest: 'manifest.json',  
    rollupOptions: {  
      input: resolve(__dirname, 'index.html'),  
    },  
  },  
  server: {  
    port: 5173,  
    open: true,  
    proxy: {  
      '/api': 'http://localhost:8000',  
      '/ws': {  
        target: 'ws://localhost:8000',  
        ws: true,  
      },  
    },  
  },  
});  