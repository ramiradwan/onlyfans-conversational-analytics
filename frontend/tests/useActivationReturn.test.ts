import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { useActivationReturn } from '../src/services/useActivationReturn';

const data = vi.hoisted(() => ({
  start: vi.fn(), check: vi.fn(), listeners: new Set<() => void>(),
  view: null as null | { sources: { brain: {
    certain: boolean; epoch: string; revision: number; snapshot: { journey_id: string };
  } } },
}));
vi.mock('../src/services/activationReturn', () => ({ activationReturnApi: data }));
vi.mock('../src/services/onboardingSession', () => ({
  onboardingView: () => data.view,
  subscribeOnboarding: (listener: () => void) => {
    data.listeners.add(listener); return () => data.listeners.delete(listener);
  },
}));
const journey = '00000000-0000-4000-8000-000000000001';
function publish(revision: number, journeyId = journey) {
  data.view = { sources: { brain: { certain: true, epoch: 'current', revision,
    snapshot: { journey_id: journeyId } } } };
  data.listeners.forEach((listener) => listener());
}
beforeEach(() => { vi.resetAllMocks(); data.view = null; data.listeners.clear(); });
afterEach(cleanup);

it('continues waiting activation on a new exact owner revision without polling', async () => {
  data.start.mockResolvedValueOnce('waiting').mockResolvedValueOnce('checking');
  const { result } = renderHook(() => useActivationReturn(journey));
  await waitFor(() => expect(result.current.state).toBe('waiting'));
  act(() => publish(1, '00000000-0000-4000-8000-000000000002'));
  expect(data.start).toHaveBeenCalledTimes(1);
  act(() => publish(1));
  await waitFor(() => expect(result.current.state).toBe('checking'));
  act(() => { publish(1); publish(2); });
  expect(data.start).toHaveBeenCalledTimes(2);
});

it('reconciles an owner event that arrives during the initial waiting read', async () => {
  let finish!: (state: string) => void;
  data.start.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
    .mockResolvedValueOnce('checking');
  const { result } = renderHook(() => useActivationReturn(journey));
  act(() => publish(1));
  await act(async () => finish('waiting'));
  await waitFor(() => expect(result.current.state).toBe('checking'));
  expect(data.start).toHaveBeenCalledTimes(2);
});

it('retires its subscriber and pending mutation permission on unmount', async () => {
  let finish!: (state: string) => void;
  data.start.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const { unmount } = renderHook(() => useActivationReturn(journey));
  const current = data.start.mock.calls[0][1] as () => boolean;
  expect(current()).toBe(true);
  unmount();
  expect(current()).toBe(false);
  act(() => publish(1));
  await act(async () => finish('waiting'));
  expect(data.start).toHaveBeenCalledTimes(1);
  expect(data.listeners.size).toBe(0);
});

it('restores owner-driven continuation when Check again finds a waiting approval', async () => {
  data.start.mockResolvedValueOnce('unconfirmed').mockResolvedValueOnce('checking');
  data.check.mockResolvedValueOnce('waiting');
  const { result } = renderHook(() => useActivationReturn(journey));
  await waitFor(() => expect(result.current.state).toBe('unconfirmed'));
  await act(async () => result.current.check());
  expect(result.current.state).toBe('waiting');
  expect(data.start).toHaveBeenCalledTimes(1);
  act(() => publish(1));
  await waitFor(() => expect(result.current.state).toBe('checking'));
  expect(data.start).toHaveBeenCalledTimes(2);
  expect(data.check).toHaveBeenCalledTimes(1);
});
