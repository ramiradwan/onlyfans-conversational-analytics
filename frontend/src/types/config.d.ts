/**  
 * Global configuration type injected by FastAPI's Jinja template  
 * into app/templates/index.html via:  
 *   <script>window.FASTAPI_CONFIG = {{ config | tojson }};</script>  
 */  
export interface FastAPIConfig {  
  EXTENSION_ID: string;  
  FASTAPI_WS_URL: string;  
}  
  
declare global {  
  interface Window {  
    FASTAPI_CONFIG: FastAPIConfig;  
  }  
}  
  
export {};  