// utils/index.ts  
import type { FastAPIConfig } from "../types/config";  
  
/**  
 * Reads global config injected by FastAPI into index.html  
 */  
declare global {  
  interface Window {  
    __FASTAPI_CONFIG__?: Partial<FastAPIConfig>;  
  }  
}  
  
export function getConfig(): FastAPIConfig {  
  const injected: Partial<FastAPIConfig> = window.__FASTAPI_CONFIG__ || {};  
  return {  
    EXTENSION_ID: injected.EXTENSION_ID ?? "dev-extension-id",  
    FASTAPI_WS_URL: injected.FASTAPI_WS_URL ?? "ws://localhost:8000/api/ws/frontend",  
  };  
}  
  
/**  
 * Removes HTML tags from a string and trims whitespace  
 */  
export function cleanText(input?: string | null): string {  
  if (!input) return "";  
  return input.replace(/<\/?[^>]+(>|$)/g, "").trim();  
}  