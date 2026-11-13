import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { payload, observation, id } from '../../extension/test-fixtures/browser-task.mjs';
import { connectExtension, reattachExtension } from '../browser-tasks/extension.js';
import { BrowserTaskClient, STREAM_POLL_MS } from '../browser-tasks/client.js';
import { readBrowserTaskEvents } from '../browser-tasks/events.js';

const event = () => { const listeners = []; return { listeners, addListener: fn => listeners.push(fn), emit: value => listeners.forEach(fn => fn(value)) }; };
const session = () => ({ role: 'browser_task', generation: id(4), task_id: id(1), journey_id: id(2), csrf_token: 'A'.repeat(43) });
const authenticationPayload = () => {
  const value = payload(); value.task.purpose = 'browser.authentication'; value.creator_account_id = 'synthetic-creator';
  value.ceremony = { entry_url: 'https://provider.example/onboarding/browser-ceremonies/' + 'A'.repeat(43),
    ceremony_id: '0199b234-5678-7000-8000-000000000002', attempt_id: 'A'.repeat(43),
    completion: { mode: 'extension-auth-flow', return_uri: `https://${value.extension_id}.chromiumapp.org/browser-ceremony/v1/complete` },
    expires_at: new Date(Date.now() + 30000).toISOString() };
  return value;
};

// A controllable helper stream: open it, push frames, or drop it.
function stream() {
  const state = { opened: 0, frame: null, drop: null };
  const events = vi.fn(({ open, frame, signal }) => new Promise((resolve, reject) => {
    state.opened++; state.frame = frame; state.drop = () => reject(Error('lost'));
    signal.addEventListener('abort', () => resolve(), { once: true });
    open();
  }));
  return { state, events };
}

function helper({ value = authenticationPayload(), link = {}, respond = () => null, events } = {}) {
  const received = [], posts = [];
  let completed = false, publish;
  const request = vi.fn(async (path, options = {}) => {
    if (options.method === 'POST') posts.push([path.split('/').at(-1), JSON.parse(options.body)]);
    const custom = respond(path, options);
    if (custom) return custom;
    const body = path.endsWith('/session') ? session()
      : path.endsWith('/payload') ? (completed ? { ...value, task: { ...value.task, state: 'completed' },
        ceremony: value.task.purpose === 'browser.authentication' ? null : value.ceremony } : value)
        : { state: 'recorded' };
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  });
  const finish = vi.fn(async () => true), close = vi.fn(), end = vi.fn(() => true), focus = vi.fn(async () => true);
  const reattach = vi.fn(async () => true);
  const client = new BrowserTaskClient({ generation: id(4), request, reattach, events: events ?? stream().events,
    receive: next => received.push(next),
    connect: ({ receive }) => { publish = receive; return { finish, close, end, focus, ...link }; } });
  return { client, request, received, posts, finish, close, end, focus, reattach, value,
    publish: next => publish(next), complete: () => { completed = true; },
    reads: () => request.mock.calls.filter(([path]) => path.endsWith('/payload')).length };
}
const frame = (state = 'completed') => ({ type: 'task', task_id: id(1), admission_generation: id(4), state });

describe('helper stream', () => {
  it('finishes a completed task only after its payload read confirms the frame', async () => {
    const s = stream(), h = helper({ events: s.events });
    try {
      await h.client.start();
      s.state.frame(frame('accepted')); await h.client.serial;
      expect(h.finish).not.toHaveBeenCalled();
      s.state.frame(frame('completed')); await h.client.serial;
      expect(h.finish).not.toHaveBeenCalled();
      expect(h.received.at(-1).state).not.toBe('task_completed');
      h.complete();
      const before = h.reads();
      s.state.frame(frame('completed')); await h.client.serial;
      expect(h.reads() - before).toBe(1);
      expect(h.received.at(-1).state).toBe('task_completed');
      expect(h.finish).toHaveBeenCalledOnce();
    } finally { h.client.stop(); }
  });

  it('ignores a frame for another task', async () => {
    const s = stream(), h = helper({ events: s.events });
    try {
      await h.client.start(); h.complete();
      const before = h.reads();
      s.state.frame({ ...frame(), task_id: id(99) }); await h.client.serial;
      expect(h.reads()).toBe(before);
      expect(h.finish).not.toHaveBeenCalled();
    } finally { h.client.stop(); }
  });

  it('polls at the safety interval while the stream is healthy and falls back at once when it drops', async () => {
    vi.useFakeTimers();
    const s = stream(), h = helper({ events: s.events });
    try {
      await h.client.start();
      let before = h.reads();
      await vi.advanceTimersByTimeAsync(STREAM_POLL_MS - 100);
      expect(h.reads()).toBe(before);
      await vi.advanceTimersByTimeAsync(100);
      expect(h.reads() - before).toBe(1);
      s.state.drop(); await vi.advanceTimersByTimeAsync(0);
      before = h.reads();
      await vi.advanceTimersByTimeAsync(500);
      expect(h.reads() - before).toBe(1);
      // It reconnects after one second, and the safety interval returns.
      await vi.advanceTimersByTimeAsync(1000);
      expect(s.state.opened).toBe(2);
      before = h.reads();
      await vi.advanceTimersByTimeAsync(5000);
      expect(h.reads()).toBe(before);
    } finally { h.client.stop(); vi.useRealTimers(); }
  });

  it('confirms focus to Brain only after the extension focused this helper', async () => {
    const s = stream(), h = helper({ events: s.events });
    try {
      await h.client.start();
      h.publish({ state: 'ready', observation: observation() });
      s.state.frame({ type: 'focus', task_id: id(1), admission_generation: id(4) }); await h.client.serial;
      expect(h.focus).toHaveBeenCalledOnce();
      expect(h.posts.filter(([path]) => path === 'focused')).toEqual([['focused', {}]]);
      h.focus.mockResolvedValueOnce(false);
      s.state.frame({ type: 'focus', task_id: id(1), admission_generation: id(4) }); await h.client.serial;
      expect(h.posts.filter(([path]) => path === 'focused')).toHaveLength(1);
    } finally { h.client.stop(); }
  });
});

describe('helper ending', () => {
  const settle = async h => { await h.client.serial; await h.client.ending; };

  it.each(['profile_mismatch', 'account_mismatch'])('reports %s to Brain before asking the extension to close it', async reason => {
    const order = [];
    const h = helper({ respond: path => { if (path.endsWith('/ended')) order.push('ended'); return null; },
      link: { end: vi.fn(() => { order.push('close'); return true; }) } });
    try {
      await h.client.start();
      h.publish({ state: reason }); await settle(h);
      expect(h.posts).toEqual([['ended', { reason }]]);
      expect(order).toEqual(['ended', 'close']);
    } finally { h.client.stop(); }
  });

  it('keeps an extension error shown when Brain did not record it', async () => {
    const h = helper({ respond: path => path.endsWith('/ended') ? { ok: false, status: 503, text: async () => '{}' } : null });
    try {
      await h.client.start();
      h.publish({ state: 'profile_mismatch' }); await settle(h);
      expect(h.end).not.toHaveBeenCalled();
      expect(h.reattach).not.toHaveBeenCalled();
      expect(h.received.at(-1).state).toBe('profile_mismatch');
    } finally { h.client.stop(); }
  });

  it('closes a changed task even when its fenced admission can no longer report', async () => {
    const h = helper({ respond: path => path.endsWith('/ended') ? { ok: false, status: 409, text: async () => '{}' } : null });
    try {
      await h.client.start();
      h.publish({ state: 'task_changed' }); await settle(h);
      expect(h.end).toHaveBeenCalledOnce();
    } finally { h.client.stop(); }
  });

  it('stays as the install surface when no extension ever answered', async () => {
    const h = helper();
    try {
      await h.client.start();
      h.publish({ state: 'unavailable' }); await settle(h);
      expect(h.posts).toEqual([['ended', { reason: 'extension_unavailable' }]]);
      expect(h.end).not.toHaveBeenCalled();
      expect(h.reattach).not.toHaveBeenCalled();
    } finally { h.client.stop(); }
  });

  it('reattaches once to close when its port is already gone, and reports only once', async () => {
    const h = helper({ link: { end: vi.fn(() => false) } });
    try {
      await h.client.start();
      h.publish({ state: 'ready', observation: observation() });
      h.publish({ state: 'account_mismatch' }); await settle(h);
      h.publish({ state: 'account_mismatch' }); await settle(h);
      expect(h.posts.filter(([path]) => path === 'ended')).toHaveLength(1);
      expect(h.reattach).toHaveBeenCalledExactlyOnceWith({ payload: h.value, type: 'close' });
    } finally { h.client.stop(); }
  });

  it('finishes through one fresh attachment when its port died before completion', async () => {
    const h = helper({ link: { finish: vi.fn(async () => false) } });
    try {
      await h.client.start(); h.complete();
      await h.client.open();
      await vi.waitFor(() => expect(h.reattach).toHaveBeenCalledOnce());
      expect(h.reattach).toHaveBeenCalledWith({ payload: h.value, type: 'finish' });
      expect(h.received.at(-1).state).toBe('task_completed');
    } finally { h.client.stop(); }
  });
});

describe('helper port', () => {
  const port = () => ({ onMessage: event(), onDisconnect: event(), postMessage: vi.fn(), disconnect: vi.fn() });

  it('keeps a refused port so the helper can ask to be closed, then lets it go', async () => {
    vi.useFakeTimers();
    try {
      const value = payload(), receive = vi.fn(), p = port();
      const link = connectExtension({ payload: value, receive, runtime: { connect: () => p } });
      p.onMessage.emit({ type: 'state', observation: observation(value.task) });
      p.onMessage.emit({ type: 'refused', reason: 'profile_mismatch' });
      expect(receive.mock.calls.at(-1)[0]).toEqual({ state: 'profile_mismatch' });
      link.close();
      expect(p.disconnect).not.toHaveBeenCalled();
      expect(link.end()).toBe(true);
      expect(p.postMessage).toHaveBeenLastCalledWith({ type: 'close', task_id: value.task.task_id, admission_generation: value.task.admission_generation });
      expect(link.end()).toBe(false);
      await vi.advanceTimersByTimeAsync(5000);
      expect(p.disconnect).toHaveBeenCalledOnce();
    } finally { vi.useRealTimers(); }
  });

  it('resolves focus only on this helper\'s own acknowledgement', async () => {
    vi.useFakeTimers();
    try {
      const value = payload(), p = port();
      const link = connectExtension({ payload: value, receive: vi.fn(), runtime: { connect: () => p } });
      p.onMessage.emit({ type: 'state', observation: observation(value.task) });
      const focused = link.focus();
      expect(p.postMessage).toHaveBeenLastCalledWith({ type: 'focus', task_id: value.task.task_id, admission_generation: value.task.admission_generation });
      p.onMessage.emit({ type: 'focused', task_id: value.task.task_id, admission_generation: value.task.admission_generation });
      expect(await focused).toBe(true);
      const late = link.focus();
      await vi.advanceTimersByTimeAsync(2000);
      expect(await late).toBe(false);
      // An acknowledgement for another task breaks this closed port.
      const foreign = link.focus();
      p.onMessage.emit({ type: 'focused', task_id: id(99), admission_generation: value.task.admission_generation });
      expect(await foreign).toBe(false);
      expect(p.disconnect).toHaveBeenCalledOnce();
    } finally { vi.useRealTimers(); }
  });

  it('sends one attach and one close on a fresh port', async () => {
    const value = payload(), p = port();
    const done = reattachExtension({ payload: value, type: 'close', runtime: { connect: () => p }, timeout: 10 });
    expect(p.postMessage.mock.calls.map(([message]) => message.type)).toEqual(['attach', 'close']);
    expect(await done).toBe(false);
    expect(p.disconnect).toHaveBeenCalledOnce();
    expect(await reattachExtension({ payload: value, type: 'open', runtime: { connect: () => p } })).toBe(false);
  });
});

describe('helper stream reader', () => {
  const response = text => ({ ok: true, headers: new Headers({ 'content-type': 'text/event-stream' }),
    body: new Response(text).body });

  it('reads only exact frames for this generation', async () => {
    const frames = [];
    const text = 'event: browser-task\ndata: ' + JSON.stringify({ task_id: id(1), admission_generation: id(4), state: 'completed' })
      + '\n\n: keepalive\n\nevent: browser-task-focus\ndata: ' + JSON.stringify({ task_id: id(1), admission_generation: id(4) }) + '\n\n';
    const request = vi.fn(async () => response(text));
    await readBrowserTaskEvents({ request, generation: id(4), frame: value => frames.push(value) });
    expect(request.mock.calls[0][1].headers['X-Desktop-Generation']).toBe(id(4));
    expect(frames.map(value => value.type)).toEqual(['task', 'focus']);
    for (const bad of [{ task_id: id(1), admission_generation: id(5), state: 'completed' },
      { task_id: id(1), admission_generation: id(4), state: 'done' },
      { task_id: id(1), admission_generation: id(4), state: 'completed', outcome: 'admitted' }]) {
      await expect(readBrowserTaskEvents({ request: async () => response('event: browser-task\ndata: ' + JSON.stringify(bad) + '\n\n'),
        generation: id(4), frame: () => {} })).rejects.toThrow('stream_invalid');
    }
  });
});

describe('helper page', () => {
  it('opens the extension listing in place and offers no second Chrome route', () => {
    const html = readFileSync('browser-tasks/index.html', 'utf8');
    expect(html).not.toContain('target="_blank"');
    expect(html).not.toContain('Continue in Chrome');
    expect(readFileSync('browser-tasks/view.js', 'utf8')).not.toContain('#system-browser');
  });
});
