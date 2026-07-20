// src/routing/useAppRoutes.tsx  
import React from 'react';
import { Navigate, RouteObject } from 'react-router-dom';
import { GlobalLoader } from '../common/GlobalLoader';
import { usePermissions } from '../hooks/usePermissions';
import { useRoleResolved } from '../store/userStore';
  
// Lazy-load views — must have default exports  
const CreatorDashboardView = React.lazy(() => import('../views/CreatorDashboardView'));  
const OperatorInboxView = React.lazy(() => import('../views/OperatorInboxView'));  
const AnalyticsView = React.lazy(() => import('../views/AnalyticsView'));  
const GraphExplorerView = React.lazy(() => import('../views/GraphExplorerView'));  
const SettingsView = React.lazy(() => import('../views/SettingsView'));
  
/**  
 * Role-based routing (Spec 11.2a)  
 */  
export const useAppRoutes = (): RouteObject[] => {
  const {
    canViewAnalytics,
    canViewDashboard,
    canViewInbox,
    canViewGraphExplorer,
    canViewSettings,
    isOperator,
  } = usePermissions();
  const roleResolved = useRoleResolved();

  // The Brain session resolves the role after the first render (see App.tsx). Until
  // then, every role-gated route is absent from the table below, so a hard load or
  // refresh of a gated deep link (e.g. /inbox) would fall through to the catch-all
  // and redirect to "/" before the role ever had a chance to resolve. Wait for
  // resolution instead of building a route table that omits the gated routes.
  if (!roleResolved) {
    return [{ path: '*', element: <GlobalLoader /> }];
  }

  const routes: RouteObject[] = [
    {  
      index: true,  
      element: isOperator  
        ? <Navigate to="/inbox" replace />  
        : canViewDashboard
          ? <CreatorDashboardView />
          : <GlobalLoader />,
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
    ...(canViewSettings
      ? [{ path: 'settings', element: <SettingsView /> }]
      : []),
    {  
      path: '*',  
      element: <Navigate to="/" replace />,  
    },  
  ];  
  
  return routes;  
};
