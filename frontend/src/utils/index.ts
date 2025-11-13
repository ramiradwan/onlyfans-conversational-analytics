import type { FastAPIConfig } from "../types/config";  
  
/**  
 * Reads config injected by FastAPI into index.html via:  
 * <script id="fastapi-config" type="application/json">{...}</script>  
 */  
export function getConfig(): FastAPIConfig {  
  const el = document.getElementById("fastapi-config");  
  if (!el) {  
    throw new Error("[CONFIG] fastapi-config element not found in index.html");  
  }  
  
  try {  
    const injected = JSON.parse(el.textContent || "{}") as Partial<FastAPIConfig>;  
  
    return {  
      EXTENSION_ID: injected.EXTENSION_ID ?? "dev-extension-id",  
      FASTAPI_WS_URL:  
        injected.FASTAPI_WS_URL ??  
        "ws://localhost:8000/ws/frontend/demo_user",  
      API_BASE_URL: injected.API_BASE_URL ?? "http://localhost:8000",  
      VERSION: injected.VERSION ?? "dev",  
      USER_ID: injected.USER_ID ?? undefined,           // <-- added  
      CREATOR_ID: injected.CREATOR_ID ?? undefined      // <-- added  
    };  
  } catch (err) {  
    console.error("[CONFIG] Failed to parse injected FastAPI config", err);  
    throw err;  
  }  
}  
  
/**  
 * Removes HTML tags from a string and trims whitespace.  
 */  
export function cleanText(input?: string | null): string {  
  if (!input) return "";  
  return input.replace(/<\/?[^>]+(>|$)/g, "").trim();  
}  