import { Alert, Box, Button, Paper, Stack, Typography } from '@mui/material';
import { useState } from 'react';

import { passkeyWindow } from '../../shared-setup/passkey-window.js';
import { getConfig } from '../config/fastapiConfig';

export default function AccountView() {
  const [opening, setOpening] = useState(false);
  const [locking, setLocking] = useState(false);
  const [error, setError] = useState('');
  const host = globalThis as typeof globalThis & {
    __TAURI__?: { core?: { invoke(command: string): Promise<unknown> } };
  };
  const native = host.__TAURI__?.core;
  const accountPasskeys = getConfig().ACCOUNT_PASSKEY_ENABLED;

  async function lockDesktop() {
    if (locking) return;
    setLocking(true);
    setError('');
    try {
      const csrf = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
      if (!csrf) throw new Error();
      await passkeyWindow('discard_passkey_unlock');
      const response = await fetch('/api/v1/webauthn/lock', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', headers: { 'X-CSRF-Token': csrf },
      });
      if (!response.ok || (await response.json()).locked !== true) throw new Error();
      window.location.replace('/');
    } catch {
      setError('Couldn’t confirm that Dipsy is locked. Try again.');
      setLocking(false);
    }
  }

  async function managePasskeys() {
    if (!native || opening) return;
    setOpening(true);
    setError('');
    try {
      await native.invoke('desktop_account_passkeys');
    } catch {
      setError('Couldn’t open Chrome. Try again.');
    } finally {
      setOpening(false);
    }
  }

  return (
    <Stack data-journey-state="desktop.account" spacing={3}
      sx={{ width: '100%', maxWidth: 720, mx: 'auto', p: { xs: 2, sm: 3 } }}>
      <Typography component="h1" variant="h4">Account</Typography>
      <Paper variant="outlined" sx={{ p: { xs: 2, sm: 3 } }}>
        <Stack spacing={2}>
          <Box>
            <Typography component="h2" variant="h6">This computer</Typography>
            <Typography color="text.secondary">Dipsy is unlocked on this computer.</Typography>
            {accountPasskeys && <Button variant="outlined" sx={{ mt: 1 }} disabled={locking}
              onClick={() => void lockDesktop()}>Lock Dipsy</Button>}
          </Box>
          {accountPasskeys && <Box>
            <Typography component="h2" variant="h6">Dipsy passkeys</Typography>
            <Typography color="text.secondary" sx={{ mb: 1 }}>
              View or remove your account passkeys in your browser.
            </Typography>
            {native
              ? <Button variant="outlined" disabled={opening} onClick={() => void managePasskeys()}>Manage passkeys</Button>
              : <Button variant="outlined" component="a" href="/account/manage-passkeys" rel="noreferrer">Manage passkeys</Button>}
          </Box>}
          {error && <Alert severity="error">{error}</Alert>}
        </Stack>
      </Paper>
    </Stack>
  );
}
