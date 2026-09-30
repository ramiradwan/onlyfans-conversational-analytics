export const REQUEST_CATEGORIES = Object.freeze(['canary_list', 'catchup_list', 'catchup_messages',
  'history_list', 'history_messages', 'identity', 'retries']);
export const emptyCounters = () => Object.fromEntries(REQUEST_CATEGORIES.map(key => [key, 0]));

export class AcquisitionAllowance {
  constructor({ outbox, clock = Date.now, dailyCap = 1000 }) {
    Object.assign(this, { outbox, clock, dailyCap });
  }

  update(work) { return this.outbox.updateAcquisitionState(work); }

  async reserve(category, limit, { automatic = false, retry = false } = {}) {
    let allowed = false;
    await this.update(state => {
      const now = this.clock();
      const minute = Math.floor(now / 60_000);
      const day = new Date(now).toISOString().slice(0, 10);
      state.days ??= {};
      const daily = state.days[day] ??= { requests: emptyCounters(), automatic: 0 };
      if (state.minute !== minute) { state.minute = minute; state.used = 0; }
      const page = category !== 'identity';
      const reserveHistory = automatic && state.history_pending === true
        && now - (state.history_at ?? 0) >= 300_000 ? 1 : 0;
      if (page && state.used >= Math.max(0, limit - reserveHistory)) return;
      if (automatic && daily.automatic >= this.dailyCap) return;
      if (page) state.used++;
      if (automatic) daily.automatic++;
      if (category.startsWith('history_')) state.history_at = now;
      daily.requests[category]++;
      if (retry) daily.requests.retries++;
      allowed = true;
    });
    return allowed;
  }

  historyPending(value) { return this.update(state => { state.history_pending = value; }); }
}
