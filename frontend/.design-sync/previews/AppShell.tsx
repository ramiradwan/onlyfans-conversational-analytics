import { Alert, Typography } from '@mui/material';
import {
  AppShell,
  MemoryRouter,
  Route,
  Routes,
} from 'onlyfans-analytics-frontend';

export function AnalyticsShell() {
  return (
    <MemoryRouter initialEntries={['/analytics']}>
      <Routes>
        <Route element={<AppShell />}>
          <Route
            path="/analytics"
            element={
              <Alert severity="info" sx={{ mt: 2 }}>
                <Typography variant="subtitle2">Analytics workspace</Typography>
                <Typography variant="body2">
                  Routed content fills the persistent navigation shell.
                </Typography>
              </Alert>
            }
          />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}
