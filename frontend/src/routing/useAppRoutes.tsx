// src/routing/useAppRoutes.tsx  
import React from 'react';  
import { Navigate, RouteObject } from 'react-router-dom';  
import { usePermissions } from '../hooks/usePermissions';  
  
// Lazy-load views — must have default exports  
const CreatorDashboardView = React.lazy(() => import('../views/CreatorDashboardView'));  
const OperatorInboxView = React.lazy(() => import('../views/OperatorInboxView'));  
const AnalyticsView = React.lazy(() => import('../views/AnalyticsView'));  
const GraphExplorerView = React.lazy(() => import('../views/GraphExplorerView'));  
  
/**  
 * Role-based routing (Spec 11.2a)  
 */  
export const useAppRoutes = (): RouteObject[] => {  
  const {  
    canViewAnalytics,  
    canViewInbox,  
    canViewGraphExplorer,  
    isOperator,  
  } = usePermissions();  
  
  const routes: RouteObject[] = [  
    {  
      index: true,  
      element: isOperator  
        ? <Navigate to="/inbox" replace />  
        : <CreatorDashboardView />,  
    },  
    ...(canViewInbox  
      ? [{ path: 'inbox', element: <OperatorInboxView /> }]  
      : []),  
    ...(canViewAnalytics  
      ? [{ path: 'analytics', element: <AnalyticsView /> }]  
      : []),  
    ...(canViewGraphExplorer  
      ? [{ path: 'graph-explorer', element: <GraphExplorerView /> }]  
      : []),  
    {  
      path: '*',  
      element: <Navigate to="/" replace />,  
    },  
  ];  
  
  return routes;  
};  