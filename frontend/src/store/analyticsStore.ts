import { createStore, type StoreApi, useStore } from 'zustand';

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
  activate(): void;
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
}

const loadingState = (): AnalyticsReadState => ({
  status: 'loading',
  data: null,
  isRefreshing: false,
  message: 'Loading canonical analytics…',
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
          : 'Canonical analytics could not be loaded.';
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
                ? 'Directional baseline — not calibrated production analysis.'
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
            message: status === 'baseline' ? 'Directional baseline — not calibrated production analysis.' : null,
          },
        });
      } catch (error) {
        handleError(error, sequence, priorData, priorStatus, allowPriorFrame);
      } finally {
        if (sequence === requestSequence) activeRequest = null;
      }
    };

    const actions: AnalyticsStoreActions = {
      activate() {
        if (active) return;
        active = true;
        set({ state: loadingState() });
        void refresh();
      },
      deactivate() {
        active = false;
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
