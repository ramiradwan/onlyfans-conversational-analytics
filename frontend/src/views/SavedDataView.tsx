import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, Link, List,
  ListItem, ListItemText, Paper, Stack, Typography } from '@mui/material';
import { useCallback, useEffect, useRef, useState } from 'react';

import { requestLocalAssertion, windowsHelloHeaders, type LoginOptions } from '../../shared-setup/webauthn';
import { OnboardingWorkspace } from '../components/OnboardingWorkspace';
import { exportSavedJson } from '../services/savedDataExport';

interface Archive {
  messages: { id: string; text: string | null; sent_at: string }[];
  next: string | null;
  deletion_pending: boolean;
}

interface ResultRef { contribution_id: string; kind: 'Full' | 'Preview' }
interface ResultPage { items: ResultRef[]; next: string | null }

class Locked extends Error {}
class HelloRefused extends Error {}
class NoSavedMetrics extends Error {}

interface SavedMetrics {
  creator_metrics: { conversation_count: number; message_count: number; inbound_message_count: number; outbound_message_count: number };
}

async function post(path: string, signal: AbortSignal, csrf?: string, body?: object) {
  const native = path === 'login/windows-hello' ? await windowsHelloHeaders(signal) : {};
  const response = await fetch(`/api/v1/saved-data/${path}`, {
    method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
    headers: { ...native, ...(csrf && { 'X-CSRF-Token': csrf }), ...(body && { 'Content-Type': 'application/json' }) },
    body: body && JSON.stringify(body),
  });
  if (path === 'login/windows-hello' && response.status === 409) {
    const { detail } = await response.json();
    const message = detail === 'permission_required' ? 'Allow Dipsy to use passkeys in Windows Settings, then try again.'
      : detail === 'foreground_required' ? 'Bring Dipsy to the front, then try again.'
        : detail === 'cancelled' ? 'Windows Hello was cancelled.' : 'Windows Hello could not open your saved data.';
    throw new HelloRefused(message);
  }
  if ([401, 403, 503].includes(response.status)) throw new Locked();
  if (path === 'metrics' && response.status === 404) throw new NoSavedMetrics();
  if (!response.ok) throw new Error('Request failed');
  return response;
}

export function SavedDataView() {
  const [archive, setArchive] = useState<Archive | null>(null);
  const [session, setSession] = useState<{ csrf: string; deadline: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<'all' | { message: string } | null>(null);
  const [results, setResults] = useState<ResultPage | null>(null);
  const [shownResult, setShownResult] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<SavedMetrics | null>(null);
  const [notice, setNotice] = useState('');
  const current = useRef<AbortController | null>(null);
  const lock = useCallback(() => {
    current.current?.abort();
    current.current = null;
    setBusy(false); setConfirming(null); setArchive(null); setSession(null); setNotice('');
    setResults(null); setShownResult(null); setMetrics(null);
  }, []);
  useEffect(() => {
    window.addEventListener('pagehide', lock);
    return () => { window.removeEventListener('pagehide', lock); current.current?.abort(); };
  }, [lock]);
  useEffect(() => {
    if (!session) return undefined;
    const timer = window.setTimeout(lock, Math.max(0, session.deadline - Date.now()));
    return () => window.clearTimeout(timer);
  }, [session, lock]);

  async function act(action: 'unlock' | 'export' | 'delete' | 'lock' | 'more' | 'results' | 'open-result' | 'export-result' | 'metrics' | 'export-metrics',
    resultRef?: ResultRef) {
    if (current.current) return;
    const controller = new AbortController();
    current.current = controller;
    const { signal } = controller;
    setBusy(true); setNotice(''); setConfirming(null);
    try {
      let csrf = session?.csrf;
      if (action === 'unlock') {
        const options: LoginOptions | { windows_hello: true } = await (await post('login/begin', signal)).json();
        let issued;
        if ('windows_hello' in options) {
          issued = await (await post('login/windows-hello', signal)).json();
        } else {
          const assertion = await requestLocalAssertion(options, signal);
          issued = await (await post('login/finish', signal, undefined, assertion)).json();
        }
        csrf = issued.csrf_token;
        setSession({ csrf: issued.csrf_token, deadline: Date.now() + issued.expires_in * 1000 });
      }
      if (!csrf) throw new Locked();
      if (action === 'lock') {
        await post('lock', signal, csrf);
        lock();
      } else if (action === 'export') {
        await exportSavedJson('creator-vault-export.json', () => post('export', signal, csrf), signal);
        if (!signal.aborted) setNotice('Saved messages exported.');
      } else if (action === 'more' && archive?.next) {
        const page: Archive = await (await post(`view?after=${encodeURIComponent(archive.next)}`, signal, csrf)).json();
        if (!signal.aborted) setArchive({ ...page, messages: [...archive.messages, ...page.messages] });
      } else if (action === 'metrics') {
        const document: SavedMetrics = await (await post('metrics', signal, csrf)).json();
        if (!signal.aborted) setMetrics(document);
      } else if (action === 'export-metrics') {
        await exportSavedJson('saved-analytics-metrics.json', () => post('metrics', signal, csrf), signal);
        if (!signal.aborted) setNotice('Saved metrics exported.');
      } else if (action === 'results') {
        const cursor = results?.next ? `?after=${encodeURIComponent(results.next)}` : '';
        const page: ResultPage = await (await post(`results${cursor}`, signal, csrf)).json();
        if (!signal.aborted) setResults({ ...page, items: [...(results?.items ?? []), ...page.items] });
      } else if ((action === 'open-result' || action === 'export-result') && resultRef) {
        const path = `results/${resultRef.kind}/${encodeURIComponent(resultRef.contribution_id)}`;
        if (action === 'export-result') {
          await exportSavedJson(`saved-analysis-${resultRef.contribution_id}.json`, () => post(path, signal, csrf), signal);
          if (!signal.aborted) setNotice('Saved result exported.');
        } else {
          const response = await post(path, signal, csrf);
          const doc = await response.json();
          const content = resultRef.kind === 'Preview' ? doc.output.summaries
            : doc.members.map((member: { record_utf8: string }) => JSON.parse(member.record_utf8));
          if (!signal.aborted) setShownResult(JSON.stringify(content, null, 2));
        }
      } else {
        let result;
        if (action === 'delete' && confirming) {
          const path = confirming === 'all' ? 'delete' : `messages/delete?message_id=${encodeURIComponent(confirming.message)}`;
          result = await (await post(path, signal, csrf)).json();
          setResults(null); setShownResult(null); setMetrics(null);
        }
        const saved: Archive = await (await post('view', signal, csrf)).json();
        if (signal.aborted) return;
        setArchive(saved);
        if (result?.status === 'complete') setNotice('Saved data deleted.');
        if (result?.deleted) setNotice('Message deleted.');
      }
    } catch (error) {
      if (signal.aborted) return;
      if ((action === 'export' || action === 'export-result' || action === 'export-metrics') && error instanceof DOMException && error.name === 'AbortError') return;
      if (error instanceof Locked || action === 'lock' || action === 'unlock') lock();
      setShownResult(null);
      setMetrics(null);
      if (error instanceof NoSavedMetrics) {
        setNotice('No saved metrics are available for this account.');
        return;
      }
      setNotice(error instanceof HelloRefused ? error.message : action === 'unlock'
        ? 'Saved data is locked. Try your existing passkey. If Windows has blocked passkeys for this app, allow them in Windows Settings and try again.'
        : error instanceof Locked ? 'Unlock your saved data again.' : 'The request did not finish. Open your saved data again to check it.');
    } finally {
      if (current.current === controller) { current.current = null; setBusy(false); }
    }
  }

  return <OnboardingWorkspace>
    <Paper variant="outlined" sx={{ p: { xs: 2, sm: 4 } }}>
      <Stack spacing={2}>
        <Typography component="h1" variant="h4">Saved data</Typography>
        {archive ? <>
          <Typography component="h2" variant="h6">Saved messages</Typography>
          {archive.messages.length ? <List aria-label="Saved messages">
            {archive.messages.map(message => <ListItem key={message.id} disableGutters>
              <ListItemText primary={message.text || 'Message without text'} secondary={message.sent_at}
                sx={{ overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }} />
              <Button color="error" disabled={busy} onClick={() => setConfirming({ message: message.id })}>Delete message</Button>
            </ListItem>)}
          </List> : <Typography>No saved messages.</Typography>}
          {archive.next && <Button disabled={busy} onClick={() => void act('more')}>Show more messages</Button>}
          <Typography component="h2" variant="h6">Saved analysis</Typography>
          <Stack direction="row" spacing={1}>
            <Button disabled={busy} onClick={() => void act('metrics')}>View saved metrics</Button>
            <Button disabled={busy} onClick={() => void act('export-metrics')}>Export saved metrics</Button>
          </Stack>
          {metrics && <Stack role="region" aria-label="Saved metrics">
            <Typography>Conversations: {metrics.creator_metrics.conversation_count}</Typography>
            <Typography>Messages: {metrics.creator_metrics.message_count}</Typography>
            <Typography>Received: {metrics.creator_metrics.inbound_message_count}</Typography>
            <Typography>Sent: {metrics.creator_metrics.outbound_message_count}</Typography>
          </Stack>}
          {results?.items.map(item => <Stack key={item.contribution_id} direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            <Typography sx={{ overflowWrap: 'anywhere' }}>{item.kind} · {item.contribution_id}</Typography>
            <Button disabled={busy} onClick={() => void act('open-result', item)}>View result</Button>
            <Button disabled={busy} onClick={() => void act('export-result', item)}>Export result</Button>
          </Stack>)}
          {results && !results.items.length && <Typography>No saved text analysis results.</Typography>}
          {(!results || results.next) && <Button disabled={busy} onClick={() => void act('results')}>
            {results ? 'Show more results' : 'Show saved results'}</Button>}
          {shownResult && <Typography component="pre" aria-label="Saved analysis result"
            sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{shownResult}</Typography>}
          {archive.deletion_pending && <Alert severity="info">Deletion still needs to finish on this computer.</Alert>}
          <Stack direction="row" useFlexGap sx={{ flexWrap: 'wrap', gap: 1 }}>
            <Button variant="outlined" disabled={busy} onClick={() => void act('export')}>Export saved data</Button>
            <Button color="error" disabled={busy}
              onClick={() => setConfirming('all')}>{archive.deletion_pending ? 'Finish deletion' : 'Delete saved data'}</Button>
            <Button disabled={busy} onClick={() => void act('lock')}>Lock saved data</Button>
          </Stack>
        </> : <>
          <Typography>Use your passkey to open your saved messages and analysis on this computer.</Typography>
          <Button variant="contained" disabled={busy} onClick={() => void act('unlock')}>
            {busy ? 'Waiting for your passkey…' : 'Unlock saved data'}
          </Button>
        </>}
        {notice && <Typography role="status">{notice}</Typography>}
        <Link href="/">Back to Dipsy</Link>
      </Stack>
    </Paper>
    <Dialog open={!!confirming} onClose={() => setConfirming(null)} aria-labelledby="saved-delete-title">
      <DialogTitle id="saved-delete-title">{confirming === 'all' ? 'Delete all saved data?' : 'Delete this message?'}</DialogTitle>
      <DialogContent>{confirming === 'all'
        ? 'This deletes all stored messages for this account on this computer and removes the analysis that uses them. This can’t be undone.'
        : 'This removes the message and the saved analysis that uses it. This can’t be undone.'}</DialogContent>
      <DialogActions>
        <Button onClick={() => setConfirming(null)}>Cancel</Button>
        <Button color="error" onClick={() => void act('delete')}>{confirming === 'all' ? 'Delete all' : 'Delete message'}</Button>
      </DialogActions>
    </Dialog>
  </OnboardingWorkspace>;
}
