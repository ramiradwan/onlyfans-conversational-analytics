import { useCallback, useEffect, useRef, useState } from 'react';

import { activationReturnApi, type ActivationReturnState } from './activationReturn';
import { onboardingView, subscribeOnboarding } from './onboardingSession';

export function useActivationReturn(journeyId: string | null) {
  const [result, setResult] = useState<{ journeyId: string; state: ActivationReturnState } | null>(null);
  const active = useRef<{
    journeyId: string; generation(): number; read(state: ActivationReturnState, observed: number): void;
  } | null>(null);
  useEffect(() => {
    if (!journeyId) return;
    let current = true;
    let running = false;
    let waiting = false;
    let generation = 0;
    let ownerRevision: string | null = null;
    const reconcile = async () => {
      if (!current || running) return;
      running = true;
      const observed = generation;
      const state = await activationReturnApi.start(journeyId, () => current);
      running = false;
      waiting = state === 'waiting';
      if (!current) return;
      setResult({ journeyId, state });
      // A pushed owner change may arrive while the initial read is in flight.
      if (waiting && generation !== observed) void reconcile();
    };
    const observe = () => {
      const brain = onboardingView()?.sources.brain;
      if (!brain?.certain || brain.snapshot?.journey_id !== journeyId) return;
      const revision = `${brain.epoch}:${brain.revision}`;
      if (revision === ownerRevision) return;
      ownerRevision = revision;
      generation += 1;
      if (waiting) void reconcile();
    };
    const unsubscribe = subscribeOnboarding(observe);
    const context = { journeyId, generation: () => generation,
      read(state: ActivationReturnState, observed: number) {
        waiting = state === 'waiting';
        if (waiting && generation !== observed) void reconcile();
      } };
    active.current = context;
    observe();
    void reconcile();
    return () => { current = false; unsubscribe(); if (active.current === context) active.current = null; };
  }, [journeyId]);
  const check = useCallback(() => {
    if (!journeyId) return;
    const context = active.current;
    if (!context || context.journeyId !== journeyId) return;
    const observed = context.generation();
    void activationReturnApi.check(journeyId).then((state) => {
      if (active.current !== context) return;
      context.read(state, observed);
      setResult({ journeyId, state: state === 'ready' ? 'unconfirmed' : state });
    });
  }, [journeyId]);
  return { state: result?.journeyId === journeyId ? result.state : 'none', check } as const;
}
