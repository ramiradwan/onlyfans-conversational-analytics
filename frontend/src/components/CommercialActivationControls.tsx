import { Alert, Button, Stack, TextField, Typography } from '@mui/material';
import { useEffect, useRef, useState } from 'react';

import { Panel } from './ui';
import {
  CAPABILITY_LICENSE_CONTINUATION_PATTERN,
  capabilityLicenseApi,
  type CapabilityLicenseApi,
  type CapabilityLicenseReadiness,
} from '../services/capabilityLicenseApi';

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Activation could not be checked. Try again.';
}

export function CommercialActivationControls({
  api = capabilityLicenseApi,
}: {
  api?: CapabilityLicenseApi;
}) {
  const [readiness, setReadiness] = useState<CapabilityLicenseReadiness | null>(null);
  const [continuation, setContinuation] = useState('');
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
    // The API object is dependency-injected and stable for the lifetime of this surface.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  const submit = async () => {
    const value = continuation.trim();
    if (!CAPABILITY_LICENSE_CONTINUATION_PATTERN.test(value)) {
      setError('Enter the complete activation continuation and try again.');
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
      setContinuation('');
    } catch (cause) {
      if (controller.signal.aborted) return;
      redemptionFailure = cause;
    }

    try {
      const next = await api.readiness(controller.signal);
      if (controller.signal.aborted) return;
      setReadiness(next);
      if (next.commercial_authority === 'active') {
        setContinuation('');
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
          <Alert severity="info" role="status">
            <Typography component="div" fontWeight={600}>Checking activation</Typography>
            Checking current Full activation and licensed-analysis readiness on this computer.
          </Alert>
        )}

        {!checking && fullReady && (
          <Alert severity="success" role="status">
            <Typography component="div" fontWeight={600}>Full mode is ready</Typography>
            Full activation is active and licensed analysis is ready.
          </Alert>
        )}

        {!checking && activeButBlocked && (
          <Alert severity="warning" role="status">
            <Typography component="div" fontWeight={600}>Full activation active</Typography>
            Activation is active, but licensed analysis is not available right now. Existing desktop data remains available.
          </Alert>
        )}

        {!checking && activationUnavailable && (
          <Alert severity="warning" role="status">
            <Typography component="div" fontWeight={600}>Full activation needs attention</Typography>
            Current activation could not be confirmed. Existing verified activation is left unchanged.
          </Alert>
        )}

        {!checking && readiness === null && (
          <Alert severity="warning" role="status">
            <Typography component="div" fontWeight={600}>Full activation needs attention</Typography>
            Activation status could not be checked. Existing verified activation is left unchanged.
          </Alert>
        )}

        {error && <Alert severity="error" role="alert">{error}</Alert>}

        {!checking && activationRequired && (
          <Stack spacing={1.5}>
            <Typography component="h3" variant="subtitle1">Full activation required</Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              Paste the activation continuation from secure setup. This computer will verify it before Full readiness changes.
            </Typography>
            <TextField
              autoComplete="off"
              label="Activation continuation"
              onChange={(event) => {
                setContinuation(event.target.value);
                if (error) setError(null);
              }}
              value={continuation}
            />
            <Button onClick={() => void submit()} variant="contained">
              Continue activation
            </Button>
          </Stack>
        )}

        {!checking && (activationUnavailable || readiness === null) && (
          <Button onClick={checkReadiness} variant="outlined">Check again</Button>
        )}
      </Stack>
    </Panel>
  );
}
