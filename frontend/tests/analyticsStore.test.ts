import { describe, expect, it, vi } from 'vitest';

import { AnalyticsClientError, type AnalyticsUpdateDocument } from '../src/analytics';
import { createAnalyticsStore } from '../src/store/analyticsStore';
import { analyticsUpdateFixture } from './analyticsFixture';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function baseline(document: AnalyticsUpdateDocument): AnalyticsUpdateDocument {
  const result = structuredClone(document);
  result.analyzer_provenance.forEach((item) => {
    item.mode = 'baseline';
    item.calibration_status = 'not_calibrated';
  });
  result.creator_metrics.provenance.mode = 'baseline';
  result.creator_metrics.provenance.calibration_status = 'not_calibrated';
  result.response_time_metrics.provenance.mode = 'baseline';
  result.response_time_metrics.provenance.calibration_status = 'not_calibrated';
  result.conversation_metrics.forEach((item) => {
    item.provenance.mode = 'baseline';
    item.provenance.calibration_status = 'not_calibrated';
  });
  return result;
}

describe('analytics session store', () => {
  it('fetches once on activate and resets to loading on deactivate', async () => {
    const store = createAnalyticsStore({
      fetchUpdate: vi.fn().mockResolvedValue(analyticsUpdateFixture()),
    });
    expect(store.getState().state.status).toBe('loading');

    await store.getState().actions.activate();
    expect(store.getState().state.status).toBe('model');

    store.getState().actions.deactivate();
    expect(store.getState().state).toMatchObject({ status: 'loading', data: null });
  });

  it('does not fetch again while already active', async () => {
    const fetchUpdate = vi.fn().mockResolvedValue(analyticsUpdateFixture());
    const store = createAnalyticsStore({ fetchUpdate });
    await store.getState().actions.activate();
    await store.getState().actions.activate();
    expect(fetchUpdate).toHaveBeenCalledTimes(1);
  });

  it('classifies baseline, building, unavailable and error states with the required retention policy', async () => {
    const fetchUpdate = vi.fn().mockResolvedValue(baseline(analyticsUpdateFixture()));
    const store = createAnalyticsStore({ fetchUpdate });
    await store.getState().actions.activate();
    expect(store.getState().state.status).toBe('baseline');

    fetchUpdate.mockRejectedValueOnce(new AnalyticsClientError({
      status: 503,
      code: 'projection_building',
      message: 'building',
      availability: 'building',
      retryable: true,
    }));
    await store.getState().actions.refresh();
    expect(store.getState().state).toMatchObject({ status: 'building', previousStatus: 'baseline' });
    expect(store.getState().state.data).not.toBeNull();

    fetchUpdate.mockRejectedValueOnce(new AnalyticsClientError({
      status: 404,
      code: 'analytics_unavailable',
      message: 'unavailable',
      availability: 'unavailable',
    }));
    await store.getState().actions.refresh();
    expect(store.getState().state).toMatchObject({ status: 'unavailable', data: null });

    fetchUpdate.mockRejectedValueOnce(new AnalyticsClientError({
      status: 401,
      code: 'authentication_failed',
      message: 'authentication failed',
    }));
    await store.getState().actions.refresh();
    expect(store.getState().state).toMatchObject({ status: 'error', data: null });
  });

  it('applies latest-request-wins and aborts stale refreshes', async () => {
    const first = deferred<AnalyticsUpdateDocument>();
    const second = deferred<AnalyticsUpdateDocument>();
    const fetchUpdate = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const store = createAnalyticsStore({ fetchUpdate });

    const activation = store.getState().actions.activate();
    const secondRefresh = store.getState().actions.refresh();
    const newest = analyticsUpdateFixture();
    newest.topics[0].volume = 9;
    second.resolve(newest);
    await secondRefresh;
    first.resolve(analyticsUpdateFixture());
    await activation;

    expect(store.getState().state.data?.topics[0].volume).toBe(9);
    expect((fetchUpdate.mock.calls[0][0].signal as AbortSignal).aborted).toBe(true);
  });

  it('rejects a start date after the end date without issuing a request', async () => {
    const fetchUpdate = vi.fn().mockResolvedValue(analyticsUpdateFixture());
    const store = createAnalyticsStore({ fetchUpdate });
    await store.getState().actions.activate();
    fetchUpdate.mockClear();

    await store.getState().actions.setDateRange({ startDate: '2026-06-30', endDate: '2026-06-01' });
    expect(fetchUpdate).not.toHaveBeenCalled();
    expect(store.getState().state).toMatchObject({ status: 'error', message: 'The start date must not be after the end date.' });
  });

  it('never touches browser storage', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem');
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    const store = createAnalyticsStore({
      fetchUpdate: vi.fn().mockResolvedValue(analyticsUpdateFixture()),
    });
    await store.getState().actions.activate();
    expect(getItem).not.toHaveBeenCalled();
    expect(setItem).not.toHaveBeenCalled();
    getItem.mockRestore();
    setItem.mockRestore();
  });
});
