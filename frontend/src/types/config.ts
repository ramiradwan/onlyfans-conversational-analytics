// src/types/config.ts  
export interface FastAPIConfig {  
  EXTENSION_ID: string;  
  FASTAPI_WS_URL: string;  
  API_BASE_URL: string;  
  VERSION: string;  
  BRIDGE_ROLE?: 'creator' | 'operator';
  USER_ID?: string;  
  CREATOR_ID?: string;  
  BRIDGE_AUTH_TICKET?: string;
}
