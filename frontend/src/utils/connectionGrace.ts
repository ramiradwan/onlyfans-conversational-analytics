import { bridgeTransportStore } from '../store/transportStore';

export type ConnectionPhase = 'connected' | 'grace' | 'interrupted';
export const CONNECTION_GRACE_MS = 3000;

export function createConnectionGrace() {
  let deadline: number | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let phase: ConnectionPhase = 'connected';
  const listeners = new Set<() => void>();
  const emit = (next: ConnectionPhase) => {
    if (phase === next) return;
    phase = next;
    listeners.forEach((listener) => listener());
  };
  const clear = () => { if (timer !== null) clearTimeout(timer); timer = null; };
  return {
    observe(connected: boolean, transient: boolean) {
      if (connected || !transient) { clear(); deadline = null; emit('connected'); return; }
      if (deadline !== null) return;
      deadline = performance.now() + CONNECTION_GRACE_MS;
      emit('grace');
      const finish = () => {
        const remaining = (deadline ?? 0) - performance.now();
        if (remaining > 0) { timer = setTimeout(finish, remaining); return; }
        timer = null;
        emit('interrupted');
      };
      timer = setTimeout(finish, CONNECTION_GRACE_MS);
    },
    getSnapshot: () => phase,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    dispose() { clear(); deadline = null; emit('connected'); },
  };
}

export const connectionGrace = createConnectionGrace();
export const connectionNote = (phase: ConnectionPhase) => phase === 'grace' ? 'Reconnecting' : phase === 'interrupted' ? 'Connection interrupted' : null;

let account = bridgeTransportStore.getState().creatorAccountId;
bridgeTransportStore.subscribe(() => {
  const state = bridgeTransportStore.getState();
  if (state.creatorAccountId !== account) { connectionGrace.dispose(); account = state.creatorAccountId; }
  if (state.viewRevision === null || state.coverage.phase === 'not_started') { connectionGrace.dispose(); return; }
  if (!state.agent) return;
  const reason = state.catchupFreshness?.reason;
  const transient = !reason || reason === 'extension_offline';
  connectionGrace.observe(state.agent.status === 'connected', transient);
});
