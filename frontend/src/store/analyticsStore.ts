import { createStore, type StoreApi, useStore } from 'zustand';

import { bridgeTransportStore, type BridgeTransportStore } from './transportStore';

import {
  AnalyticsClientError,
  AnalyticsContractError,
  adaptAnalyticsReadModel,
  classifyAnalyticsModel,
  fetchAnalyticsUpdate,
  type AnalyticsDateRange,
  type AnalyticsFrameStatus,
  type AnalyticsReadModel,
  type AnalyticsReadState,
} from '../analytics';

interface AnalyticsStoreActions {
  activate(): Promise<void>;
  deactivate(): void;
  refresh(): Promise<void>;
  setDateRange(range: AnalyticsDateRange): Promise<void>;
}

export interface AnalyticsStoreState {
  state: AnalyticsReadState;
  dateRange: AnalyticsDateRange;
  actions: AnalyticsStoreActions;
}

interface AnalyticsStoreDependencies {
  fetchUpdate?: typeof fetchAnalyticsUpdate;
  source?: Pick<BridgeTransportStore, 'getState' | 'subscribe'>;
}

const loadingState = (): AnalyticsReadState => ({
  status: 'loading',
  data: null,
  isRefreshing: false,
  message: 'Loading your analytics…',
});

function frameStatus(state: AnalyticsReadState): AnalyticsFrameStatus | null {
  if (state.status === 'baseline' || state.status === 'model') return state.status;
  if (state.status === 'building' || state.status === 'error') return state.previousStatus;
  return null;
}

function frameData(state: AnalyticsReadState): AnalyticsReadModel | null {
  return state.data;
}

function isValidRange(range: AnalyticsDateRange): boolean {
  return !range.startDate || !range.endDate || range.startDate <= range.endDate;
}

/**
 * Session-bound analytics read model. Authority comes entirely from the same-origin
 * bridge session cookie the browser attaches to `fetchAnalyticsUpdate` requests — there
 * is no ticket, account parameter, or bootstrap document to bind here. `activate`/
 * `deactivate` mirror the `websocketService.connect`/`disconnect` lifecycle in `App`.
 */
export function createAnalyticsStore(
  dependencies: AnalyticsStoreDependencies = {},
): StoreApi<AnalyticsStoreState> {
  const fetchUpdate = dependencies.fetchUpdate ?? fetchAnalyticsUpdate;
  let active = false;
  let activeRequest: AbortController | null = null;
  let requestSequence = 0;
  const source = dependencies.source ?? bridgeTransportStore;
  let unsubscribe: (() => void) | null = null;
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingInvalidation = false;
  let retryCount = 0;
  let sourceKey = '';
  const currentSourceKey = () => {
    const value = source.getState();
    return value.connection === 'connected' && value.viewRevision !== null
      ? `${value.creatorAccountId}:${value.projection.canonical_revision}:${value.projection.projected_revision}:${value.projection.status}:${value.coverage.generation_id}:${value.coverage.status}` : '';
  };

  return createStore<AnalyticsStoreState>((set, get) => {
    const invalidate = () => {
      requestSequence += 1;
      activeRequest?.abort();
      activeRequest = null;
    };

    const handleError = (
      error: unknown,
      sequence: number,
      priorData: AnalyticsReadModel | null,
      priorStatus: AnalyticsFrameStatus | null,
      allowPriorFrame: boolean,
    ) => {
      if (
        (error instanceof DOMException && error.name === 'AbortError') ||
        !active ||
        sequence !== requestSequence
      ) return;

      const message =
        error instanceof AnalyticsClientError || error instanceof AnalyticsContractError
          ? error.message
          : 'Check your connection and try again.';
      if (error instanceof AnalyticsClientError) {
        if (error.status === 401 || error.status === 403) {
          set({ state: { status: 'error', data: null, isRefreshing: false, message, previousStatus: null } });
          return;
        }
        if (error.status === 404 || (error.status === 503 && error.availability === 'unavailable')) {
          set({ state: { status: 'unavailable', data: null, isRefreshing: false, message } });
          return;
        }
        if (error.status === 503 && error.availability === 'building') {
          set({
            state: {
              status: 'building',
              data: allowPriorFrame ? priorData : null,
              isRefreshing: false,
              message,
              previousStatus: allowPriorFrame ? priorStatus : null,
            },
          });
          return;
        }
      }
      set({
        state: {
          status: 'error',
          data: allowPriorFrame ? priorData : null,
          isRefreshing: false,
          message,
          previousStatus: allowPriorFrame ? priorStatus : null,
        },
      });
    };

    const scheduleRefresh = (delay = 1000) => {
      pendingInvalidation = true;
      if (!active || activeRequest || refreshTimer !== null) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = null;
        pendingInvalidation = false;
        void refresh();
      }, delay);
    };

    const refresh = async (): Promise<void> => {
      if (!active) return;
      const { state, dateRange } = get();
      const priorData = frameData(state);
      const priorStatus = frameStatus(state);
      const allowPriorFrame = priorData !== null && priorStatus !== null;
      invalidate();
      const controller = new AbortController();
      activeRequest = controller;
      const sequence = requestSequence;

      if (!allowPriorFrame) {
        set({ state: loadingState() });
      } else {
        set({
          state: {
            status: priorStatus,
            data: priorData,
            isRefreshing: true,
            message:
              priorStatus === 'baseline'
                ? 'These results are early estimates.'
                : null,
          },
        });
      }

      try {
        const update = await fetchUpdate({ range: dateRange, signal: controller.signal });
        if (!active || sequence !== requestSequence) return;
        const model = adaptAnalyticsReadModel(update);
        const status = classifyAnalyticsModel(model);
        set({
          state: {
            status,
            data: model,
            isRefreshing: false,
            message: status === 'baseline' ? 'These results are early estimates.' : null,
          },
        });
      } catch (error) {
        handleError(error, sequence, priorData, priorStatus, allowPriorFrame);
      } finally {
        if (sequence === requestSequence) {
          activeRequest = null;
          if (pendingInvalidation) scheduleRefresh();
          else if (get().state.status === 'building' && retryCount < 5) scheduleRefresh(2000 * 2 ** retryCount++);
        }
      }
    };

    const actions: AnalyticsStoreActions = {
      async activate() {
        if (active) return;
        active = true;
        sourceKey = currentSourceKey();
        unsubscribe = source.subscribe(() => {
          const next = currentSourceKey();
          if (!next) { sourceKey = ''; return; }
          if (next === sourceKey) return;
          sourceKey = next;
          retryCount = 0;
          scheduleRefresh();
        });
        set({ state: loadingState() });
        await refresh();
      },
      deactivate() {
        active = false;
        unsubscribe?.(); unsubscribe = null;
        if (refreshTimer !== null) clearTimeout(refreshTimer);
        refreshTimer = null;
        pendingInvalidation = false;
        retryCount = 0;
        sourceKey = '';
        invalidate();
        set({ state: loadingState() });
      },
      refresh,
      async setDateRange(dateRange) {
        if (!isValidRange(dateRange)) {
          const current = get().state;
          set({
            state: {
              status: 'error',
              data: frameData(current),
              isRefreshing: false,
              message: 'The start date must not be after the end date.',
              previousStatus: frameStatus(current),
            },
          });
          return;
        }
        set({ dateRange });
        await refresh();
      },
    };

    return {
      state: loadingState(),
      dateRange: { startDate: '', endDate: '' },
      actions,
    };
  });
}

export const analyticsStore = createAnalyticsStore();
export function useAnalyticsStore<T>(selector: (state: AnalyticsStoreState) => T): T {
  return useStore(analyticsStore, selector);
}
export const analyticsStoreActions = analyticsStore.getState().actions;

export function setAnalyticsStoryState(state: AnalyticsReadState): () => void {
  if (!import.meta.env.DEV) return () => undefined;
  const previous = analyticsStore.getState();
  analyticsStore.setState({ state });
  return () => analyticsStore.setState({ state: previous.state });
}
