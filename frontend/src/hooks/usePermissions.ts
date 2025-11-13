// src/hooks/usePermissions.ts  
export interface Permissions {  
  isCreator: boolean;  
  isManager: boolean;  
  isOperator: boolean;  
}  
  
export function usePermissions(): Permissions {  
  // however you determine permissions  
  return {  
    isCreator: false,  
    isManager: false,  
    isOperator: true,  
  };  
}  