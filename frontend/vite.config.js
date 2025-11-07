import { defineConfig } from 'vite';  
import react from '@vitejs/plugin-react';  
import { resolve, dirname } from 'path';  
import { fileURLToPath } from 'url';  
  
// Fix __dirname in ES module context  
const __filename = fileURLToPath(import.meta.url);  
const __dirname = dirname(__filename);  
  
export default defineConfig({  
  plugins: [react()],  
  resolve: {  
    // Allow .ts/.tsx imports without specifying extension  
    extensions: ['.js', '.jsx', '.ts', '.tsx'],  
    alias: {  
      '@types': resolve(__dirname, 'src/types'),  
      '@components': resolve(__dirname, 'src/components'),  
      '@': resolve(__dirname, 'src')  
    }  
  },  
  root: __dirname, // frontend/ is the root  
  build: {  
    outDir: resolve(__dirname, '../app/static/dist'), // FastAPI static dir  
    emptyOutDir: true, // clear old files before build  
    manifest: 'manifest.json', // enables hashed asset mapping for Jinja  
    rollupOptions: {  
      input: resolve(__dirname, 'index.html')  
    }  
  },  
  server: {  
    port: 5173,  
    open: true  
  }  
});  