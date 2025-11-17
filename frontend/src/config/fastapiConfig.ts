import type { FastAPIConfig } from '@/types/config';
  
/**  
 * Reads config injected by FastAPI into index.html via:  
 * <script id="fastapi-config" type="application/json">{...}</script>  
 *  
 * If not found, returns sensible dev defaults.  
 */  
export function getConfig(): FastAPIConfig {  
  const el = document.getElementById('fastapi-config');  
  
  if (!el) {  
    console.warn('[CONFIG] fastapi-config element not found — using dev defaults');  
    return {  
      EXTENSION_ID: 'dev-extension-id',  
      FASTAPI_WS_URL: 'ws://localhost:8000/api/ws/frontend/demo_user',  
      API_BASE_URL: 'http://localhost:8000',  
      VERSION: 'dev',  
      USER_ID: 'demo_user',  
      CREATOR_ID: undefined,  
    };  
  }  
  
  try {  
    const injected = JSON.parse(el.textContent || '{}') as Partial<FastAPIConfig>;  
    return {  
      EXTENSION_ID: injected.EXTENSION_ID ?? 'dev-extension-id',  
      FASTAPI_WS_URL:  
        injected.FASTAPI_WS_URL ?? 'ws://localhost:8000/api/ws/frontend/demo_user',  
      API_BASE_URL: injected.API_BASE_URL ?? 'http://localhost:8000',  
      VERSION: injected.VERSION ?? 'dev',  
      USER_ID: injected.USER_ID ?? undefined,  
      CREATOR_ID: injected.CREATOR_ID ?? undefined,  
    };  
  } catch (err) {  
    console.error('[CONFIG] Failed to parse injected FastAPI config', err);  
    throw err;  
  }  
}  