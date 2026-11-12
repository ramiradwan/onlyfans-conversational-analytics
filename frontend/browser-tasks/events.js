import { exact, UUID } from '../../shared/browser-tasks/wire.mjs';
import { parseOnboardingJson } from '../../shared/onboarding/json.mjs';

export const EVENTS_PATH = '/api/v1/browser-tasks/events';
const STATES = ['pending', 'accepted', 'outcome_unknown', 'completed', 'cancelled', 'expired'];

/** One helper stream. Frames only say that this task changed; the payload read decides. */
export async function readBrowserTaskEvents({ request, generation, signal, open, frame, idleTimeoutMs = 45_000 }) {
  const response = await request(EVENTS_PATH, {
    credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal,
    headers: { Accept: 'text/event-stream', 'X-Desktop-Generation': generation },
  });
  if (!response?.ok || !response.body
    || response.headers?.get('content-type')?.split(';')[0].trim() !== 'text/event-stream') {
    throw new Error('stream_unavailable');
  }
  const reader = response.body.getReader();
  let idle;
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  const arm = () => { clearTimeout(idle); idle = setTimeout(cancel, idleTimeoutMs); };
  signal?.addEventListener('abort', cancel, { once: true });
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let pending = '', event = '', data = '';
  const line = value => {
    if (!value) {
      if (data) frame(parseFrame(event, data, generation));
      event = ''; data = ''; return;
    }
    if (value.startsWith(':')) return;
    const separator = value.indexOf(':');
    const name = separator < 0 ? value : value.slice(0, separator);
    const field = separator < 0 ? '' : value.slice(separator + 1).replace(/^ /u, '');
    if (name === 'event') event = field;
    else if (name === 'data') {
      data += (data ? '\n' : '') + field;
      if (data.length > 1024) throw new Error('stream_invalid');
    }
  };
  try {
    arm();
    open?.();
    while (!signal?.aborted) {
      const chunk = await reader.read();
      if (chunk.done) return;
      arm();
      pending += decoder.decode(chunk.value, { stream: true });
      let end;
      while ((end = pending.indexOf('\n')) >= 0) {
        line(pending.slice(0, end).replace(/\r$/u, ''));
        pending = pending.slice(end + 1);
      }
      if (pending.length > 2048) throw new Error('stream_invalid');
    }
  } finally {
    clearTimeout(idle);
    signal?.removeEventListener('abort', cancel);
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function parseFrame(event, data, generation) {
  const value = parseOnboardingJson(data);
  if (event === 'browser-task' && exact(value, ['task_id', 'admission_generation', 'state'])
    && UUID.test(value.task_id) && value.admission_generation === generation && STATES.includes(value.state)) {
    return { type: 'task', ...value };
  }
  if (event === 'browser-task-focus' && exact(value, ['task_id', 'admission_generation'])
    && UUID.test(value.task_id) && value.admission_generation === generation) {
    return { type: 'focus', ...value };
  }
  throw new Error('stream_invalid');
}
