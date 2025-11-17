import React, { Suspense } from 'react';
import { useRoutes, RouteObject } from 'react-router-dom';
import { AppShell } from '../layouts/AppShell';
import { useAppRoutes } from './useAppRoutes';
import { GlobalLoader } from '../common/GlobalLoader';

/**
 * This component defines the top-level routing structure.
 * It renders the persistent <AppShell /> layout and uses
 * the dynamic, role-based routes from useAppRoutes()
 * to render the correct view inside the shell's <Outlet />.
 */
export function AppRouter() {
  // Get the routes array based on the user's role
  const appRoutes = useAppRoutes();

  // Define the top-level route structure
  const routes: RouteObject[] = [
    {
      path: '/*', // All routes are children of the AppShell
      element: <AppShell />,
      children: appRoutes, // Render the role-based routes inside the shell
    },
  ];

  const element = useRoutes(routes);

  // Wrap the router in Suspense for lazy-loading views
  // and show a GlobalLoader as a fallback (Spec 13.0)
  return <Suspense fallback={<GlobalLoader />}>{element}</Suspense>;
}