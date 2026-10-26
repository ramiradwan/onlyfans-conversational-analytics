import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Stack, Typography } from '@mui/material';
import { useEffect, useMemo, useRef, useState } from 'react';

import { Panel, SectionHeader } from './ui';

import { engineAcquisitionApi, EngineAcquisitionError, type EngineAcquisitionApi, type EngineReview, type EngineSelection, type EngineStatus } from '../api/engineAcquisition';

const number = new Intl.NumberFormat();
const bytes = (value: number) => `${number.format(value)} bytes`;
const states: Record<EngineStatus['state'], string> = {
  not_requested: 'Engine installation has not been requested.', checking: 'Checking Engine installation.',
  acquiring: 'Windows is installing the Engine.', cancelling: 'Cancellation requested; waiting for Windows.',
  installed: 'Engine installation verified.', failed: 'Engine installation did not complete.',
  recovery_pending: 'Engine installation needs attention. Another installation cannot start.',
};
export function EngineDependencyControls({ account, api = engineAcquisitionApi, selection = 'module' }: {
  account: string | null; api?: EngineAcquisitionApi; selection?: EngineSelection;
}) {
  const context = useMemo(() => ({ account, api, selection }), [account, api, selection]);
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [reviewed, setReviewed] = useState<{ context: typeof context; value: EngineReview } | null>(null);
  const quote = reviewed?.context === context ? reviewed.value : null;
  const setQuote = (value: EngineReview | null) => setReviewed(value === null ? null : { context, value });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [fence, setFence] = useState<'ownership' | 'uncertain' | null>(null);
  const uncertainAfter = useRef<string | null>(null);
  const [revision, setRevision] = useState(0);
  const lifetime = useRef<AbortController | null>(null);
  const currentContext = useRef(context); currentContext.current = context;
  const matches = (selection === 'module' || selection === 'preview') && api.selection === selection;
  const supported = matches && api.supported();
  const current = (signal: AbortSignal) => currentContext.current === context && !signal.aborted && api.selection === selection;
  const active = () => Boolean(account && supported && lifetime.current && current(lifetime.current.signal));
  const observe = (value: EngineStatus) => {
    setStatus(value);
    if (value.state === 'recovery_pending') setFence('ownership');
    // A real recorded terminal outcome may resolve an uncertain command response.
    else if (value.operation_id && value.operation_id !== uncertainAfter.current && !value.in_flight && ['installed', 'failed'].includes(value.state)) {
      setFence((current) => current === 'uncertain' ? null : current);
    }
  };
  useEffect(() => {
    const controller = new AbortController(); lifetime.current = controller;
    setStatus(null); setQuote(null); setError(false); setBusy(false);
    if (account && supported) void api.status(controller.signal).then((value) => {
      if (current(controller.signal)) observe(value);
    }).catch(() => { if (current(controller.signal)) setError(true); });
    return () => controller.abort();
  }, [account, api, selection, supported]);
  useEffect(() => {
    if (!account || !status?.in_flight || !supported) return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await api.status(controller.signal);
        if (!current(controller.signal)) return;
        observe(value);
        if (value.in_flight) timer = setTimeout(() => { void poll(); }, 1000);
      } catch { if (current(controller.signal)) setError(true); }
    };
    timer = setTimeout(() => { void poll(); }, 1000);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [account, api, selection, status?.in_flight, supported, revision]);
  const check = async () => {
    if (!active() || busy) return;
    const signal = lifetime.current!.signal;
    setBusy(true); setError(false); setQuote(null);
    try { const value = await api.status(signal); if (current(signal)) { observe(value); setRevision((n) => n + 1); } }
    catch { if (current(signal)) setError(true); }
    finally { if (current(signal)) setBusy(false); }
  };
  const blocked = fence !== null || status?.in_flight || status?.state === 'recovery_pending';
  const review = async () => {
    if (!active() || busy || blocked || !status) return;
    const signal = lifetime.current!.signal;
    setBusy(true); setError(false); setQuote(null);
    try {
      const value = await api.review(signal);
      if (current(signal)) {
        if (value.availability === 'available' && value.explicit_selection !== selection) throw new EngineAcquisitionError('unavailable');
        setQuote(value);
      }
    }
    catch (cause) { if (current(signal)) { setError(true); if (cause instanceof EngineAcquisitionError && cause.kind === 'ownership') setFence('ownership'); } }
    finally { if (current(signal)) setBusy(false); }
  };
  const start = async () => {
    if (!active() || busy || blocked || quote?.availability !== 'available' || quote.explicit_selection !== selection) return;
    const signal = lifetime.current!.signal; const digest = quote.review_digest;
    setBusy(true); setError(false); setQuote(null);
    uncertainAfter.current = status?.operation_id ?? null;
    setFence('uncertain');
    try { const value = await api.start(digest, signal); if (current(signal)) { observe(value); setRevision((n) => n + 1); } }
    catch (cause) { if (current(signal)) { setError(true); if (cause instanceof EngineAcquisitionError && cause.kind === 'stale_review') setFence(null);
    else {
      setFence(cause instanceof EngineAcquisitionError && cause.kind === 'ownership' ? 'ownership' : 'uncertain');
    } } }
    finally { if (current(signal)) setBusy(false); }
  };
  const cancel = async () => {
    if (!active() || busy || !status?.operation_id || !status.in_flight || status.cancel_requested) return;
    const signal = lifetime.current!.signal; const id = status.operation_id;
    setBusy(true); setError(false); setQuote(null);
    try { const value = await api.cancel(id, signal); if (current(signal)) { observe(value); setRevision((n) => n + 1); } }
    catch { if (current(signal)) { setError(true); setFence('ownership'); } }
    finally { if (current(signal)) setBusy(false); }
  };
  return <Panel component="section" aria-label="Text analysis Engine" data-journey-state="textanalysis.engine_controls">
    <SectionHeader title="Text analysis Engine" />
    <Typography>The Engine is shared on this device. Installing it does not download a model profile or enable processing. Other analytics remain available.</Typography>
    <Typography>Supported languages and tasks are listed with each model profile. Review the profile before downloading its files.</Typography>
    {!matches ? <Alert severity="error">Engine setup selection could not be verified. No installation was requested.</Alert> : !supported ? <Alert severity="info">Engine installation is available in the Windows desktop app.</Alert> : !account ? <Alert severity="info">Choose an account to review Engine installation.</Alert> : <Stack spacing={1}>
      {busy && <Typography role="status">Updating Engine setup status...</Typography>}
      {error && <Alert severity="error">Engine setup could not be verified. Check status before another request.</Alert>}
      {fence !== null && <Alert severity="warning">Another installation request is unavailable while ownership or servicing remains unresolved. Other analytics and Quit remain available.</Alert>}
      {status && <Stack role="status" spacing={1}>
        <Typography>{states[status.state]}</Typography>
        {status.in_flight && <Typography>Windows owns this operation. Closing this view does not cancel it. Transfer bytes and percentage are not reported.</Typography>}
        {status.cancel_requested && <Typography>Cancellation is requested; it is not proof that Windows has finished.</Typography>}
        {status.deadline_reached && <Typography>The time budget was reached. Windows settlement may take longer.</Typography>}
        {status.installed && status.state === 'installed' && <Typography>Only Engine installation is verified. Profile preparation and processing remain separate.</Typography>}
      </Stack>}
      <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
        <Button disabled={busy} onClick={() => { void check(); }}>Check Engine status</Button>
        <Button disabled={busy || !status || Boolean(blocked) || status.state === 'installed'} onClick={() => { void review(); }}>Review Engine installation</Button>
        {status?.in_flight && <Button disabled={busy || status.cancel_requested || !status.operation_id} onClick={() => { void cancel(); }}>Cancel Engine installation</Button>}
      </Stack>
      {quote?.availability === 'unavailable' && <Alert severity="info">A verified Engine installation quote is unavailable. No installation was started.</Alert>}
    </Stack>}
    <Dialog open={matches && quote?.availability === 'available' && quote.explicit_selection === selection} onClose={() => setQuote(null)} aria-labelledby="engine-install-review-title">
      <DialogTitle id="engine-install-review-title">Review Engine installation</DialogTitle>
      <DialogContent data-journey-state="textanalysis.engine_review">{quote?.availability === 'available' && <Stack spacing={1}>
        <Typography>Setup choice: {selection === 'preview' ? 'Preview' : 'Module'}.</Typography>
        <Typography>Engine version: {quote.engine_package_version}</Typography>
        <Typography>Expected complete signed package: {bytes(quote.expected_complete_package_bytes)}</Typography>
        <Typography>Declared installed space budget: {bytes(quote.declared_disk.installed_space_budget_bytes)}</Typography>
        <Typography>Declared temporary space budget: {bytes(quote.declared_disk.temporary_space_budget_bytes)}</Typography>
        <Typography>Declared rollback space budget: {bytes(quote.declared_disk.rollback_space_budget_bytes)}</Typography>
        <Typography>Declared required free space: {bytes(quote.declared_disk.required_free_space_bytes)}</Typography>
        <Typography>These are publisher-expected package bytes and declared disk budgets. Actual Windows transfer, cache reuse and temporary disk use are not reported.</Typography>
        <Typography>This installs shared Engine code only. Review model data, language and task support separately before downloading a profile. Processing remains unavailable until its separate requirements are met.</Typography>
      </Stack>}</DialogContent>
      <DialogActions><Button onClick={() => setQuote(null)}>Back</Button><Button disabled={busy || Boolean(blocked) || quote?.availability !== 'available'} onClick={() => { void start(); }}>Install reviewed Engine</Button></DialogActions>
    </Dialog>
  </Panel>;
}
