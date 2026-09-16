import { Alert, AlertTitle, Box, Button, Stack, TextField, Typography } from '@mui/material';
import { useEffect, useRef, useState } from 'react';

import { Panel } from './ui';
import {
  CAPABILITY_LICENSE_CONTINUATION_PATTERN,
  capabilityLicenseApi,
  type CapabilityLicenseApi,
  type CapabilityLicenseReadiness,
} from '../services/capabilityLicenseApi';

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Activation couldn't be checked. Try again.";
}

/** Full activation section of Settings: activation status and activation code entry. */
export function CommercialActivationControls({
  api = capabilityLicenseApi,
}: {
  api?: CapabilityLicenseApi;
}) {
  const [readiness, setReadiness] = useState<CapabilityLicenseReadiness | null>(null);
  const [code, setCode] = useState('');
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const operation = useRef<AbortController | null>(null);

  const checkReadiness = () => {
    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setChecking(true);
    setError(null);
    void api.readiness(controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      setReadiness(next);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(safeMessage(cause));
    }).finally(() => {
      if (!controller.signal.aborted) setChecking(false);
    });
  };

  useEffect(() => {
    checkReadiness();
    return () => operation.current?.abort();
  }, [api]);

  const submit = async () => {
    const value = code.trim();
    if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(value)) {
      setError('Enter the full activation code and try again.');
      return;
    }

    operation.current?.abort();
    const controller = new AbortController();
    operation.current = controller;
    setChecking(true);
    setError(null);

    let redemptionFailure: unknown = null;
    try {
      await api.redeem(value, controller.signal);
      if (controller.signal.aborted) return;
      setCode('');
    } catch (cause) {
      if (controller.signal.aborted) return;
      redemptionFailure = cause;
    }

    try {
      const next = await api.readiness(controller.signal);
      if (controller.signal.aborted) return;
      setReadiness(next);
      if (next.commercial_authority === 'active') {
        setCode('');
        setError(null);
      } else if (redemptionFailure !== null) {
        setError(safeMessage(redemptionFailure));
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(redemptionFailure === null ? safeMessage(cause) : safeMessage(redemptionFailure));
      }
    } finally {
      if (!controller.signal.aborted) setChecking(false);
    }
  };

  const fullReady = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'admitted';
  const activeButBlocked = readiness?.commercial_authority === 'active'
    && readiness.analysis_admission === 'blocked';
  const activationRequired = readiness?.commercial_authority === 'required';
  const activationUnavailable = readiness?.commercial_authority === 'unavailable';

  return (
    <Panel>
      <Stack spacing={2}>
        <Typography component="h2" variant="h6">Full activation</Typography>

        {checking && (
          <Typography role="status" variant="body2" sx={{ color: 'text.secondary' }}>
            Checking activation…
          </Typography>
        )}

        {!checking && fullReady && (
          <Alert severity="success" role="status">Full analytics is on.</Alert>
        )}

        {!checking && activeButBlocked && (
          <Alert severity="warning" role="status">
            <AlertTitle>New messages aren&apos;t being analyzed</AlertTitle>
            Your activation is fine, but analysis can&apos;t run right now. Your existing numbers are
            still available.
          </Alert>
        )}

        {!checking && (activationUnavailable || readiness === null) && (
          <Alert severity="warning" role="status">
            Your activation couldn&apos;t be checked. Nothing has changed.
          </Alert>
        )}

        {error && <Alert severity="error" role="alert">{error}</Alert>}

        {!checking && activationRequired && (
          <Stack spacing={1.5}>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Activate to see mood, reply, and topic insights for your conversations. To get a code,
              open secure setup and choose Activate Full. Codes expire after a few minutes, so paste
              yours right away.
            </Typography>
            <TextField
              autoComplete="off"
              label="Activation code"
              onChange={(event) => {
                setCode(event.target.value);
                if (error) setError(null);
              }}
              value={code}
            />
            <Box>
              <Button onClick={() => void submit()} variant="contained">
                Activate
              </Button>
            </Box>
          </Stack>
        )}

        {!checking && (activationUnavailable || activeButBlocked || readiness === null) && (
          <Box>
            <Button onClick={checkReadiness} variant="outlined">Check again</Button>
          </Box>
        )}
      </Stack>
    </Panel>
  );
}
