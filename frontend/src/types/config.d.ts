/**  
 * Global configuration type injected by FastAPI's Jinja template  
 * into app/templates/index.html via:  
 *   <script id="fastapi-config" type="application/json">  
 *     {{ config | tojson | safe }}  
 *   </script>  
 */  
export interface FastAPIConfig {  
  EXTENSION_ID: string;  
  FASTAPI_WS_URL: string;  
  API_BASE_URL: string;  
  VERSION: string;  
  USER_ID?: string;     // <-- new, optional in case backend doesn't send  
  CREATOR_ID?: string;  // <-- new, optional in case backend doesn't send  
}  
  
declare global {  
  interface Window {  
    FASTAPI_CONFIG: FastAPIConfig;  
  }  
}  
  
export {};  