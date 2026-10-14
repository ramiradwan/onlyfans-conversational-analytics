/** Header-authenticated, bounded stream. Every event contains the entire projection. */
export async function setupEvents(request, headers, signal, receive) {
  const response = await request('/api/v1/desktop/setup/events', { credentials: 'same-origin', redirect: 'error',
    cache: 'no-store', headers: { Accept: 'text/event-stream', ...headers }, signal });
  if (!response.ok || !response.body || response.headers.get('content-type')?.split(';')[0] !== 'text/event-stream') {
    throw new Error(response.status === 403 ? 'authorization_unavailable' : 'stream_unavailable');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '', data = '', event = '', timeout;
  const cancel = () => { void reader.cancel().catch(() => {}); };
  const arm = () => { clearTimeout(timeout); timeout = setTimeout(cancel, 45_000); };
  signal.addEventListener('abort', cancel, { once: true });
  try {
    arm();
    while (!signal.aborted) {
      const chunk = await reader.read();
      if (chunk.done) break;
      arm();
      pending += decoder.decode(chunk.value, { stream: true });
      let end;
      while ((end = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0, end).replace(/\r$/u, '');
        pending = pending.slice(end + 1);
        if (!line) {
          if (event === 'setup' && data) receive(data);
          data = ''; event = '';
        } else if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /u, '');
        if (data.length > 4096 || line.length > 8192) throw new Error('invalid_stream');
      }
      if (pending.length > 8192) throw new Error('invalid_stream');
    }
    if (!signal.aborted) throw new Error('stream_unavailable');
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
