import { useUserRole } from '../store/userStore';  
  
/**  
 * Hook to determine user permissions based on their role.  
 * This abstracts the business logic for "who can see what"  
 * from the components themselves.  
 * (Based on Spec 3.1, 5.1, and 11.2)  
 */  
export const usePermissions = () => {  
  const role = useUserRole();  
  
  const isCreator = role === 'creator-ceo';  
  const isOperator = role === 'operator';  
  
  return {  
    role,  
    isCreator,  
    isOperator,  
  
    // Per Spec 5.1: "Dashboard (Creator-only)"  
    canViewDashboard: isCreator,  
  
    // Per Spec 5.1: "Analytics (Creator-only)"  
    canViewAnalytics: isCreator,  
  
    // Per Spec 5.1: "Inbox (Conversation-first)"  
    // This is the primary view for Operators and also accessible to Creators.  
    canViewInbox: isCreator || isOperator,  
  
    // NEW: Per Spec — Graph Explorer is Creator-only  
    canViewGraphExplorer: isCreator,  
  };  
};  