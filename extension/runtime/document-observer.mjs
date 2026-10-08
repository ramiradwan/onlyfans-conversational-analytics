import { PAGE_CONTROL_MESSAGE_TYPE, PAGE_CONTROL_VERSION } from '../capture/envelopes.mjs';

export const OBSERVER_STATE_TYPE = 'ofca.observer.state';
export const OBSERVER_HELPER_KEY = 'onboarding_observer_helper_v1';
export const OBSERVER_REOPEN_TYPE = 'ofca.ui.reopen-background-tab';
const ONLYFANS = 'https://onlyfans.com/*';
const HELPER_URL = 'https://onlyfans.com/';
const modes = ['identity', 'preview', 'full'];
const ownedUrl = (value) => { try { return new URL(value).origin === 'https://onlyfans.com'; } catch { return false; } };
const validStatus = (value) => value && Object.keys(value).length === 4 && modes.includes(value.mode)
  && ['active', 'forwarding', 'ws2_socket_open'].every((key) => typeof value[key] === 'boolean');

// One operation owner attaches observers and owns fallback navigation. No operation
// reloads or navigates a user tab. Cached document facts are invalidated by lifecycle
// events; reading status never scans tabs or probes documents.
export class DocumentObserverCoordinator {
  constructor({ chromeApi, scheduler = globalThis, changed = () => {} }) {
    this.chrome = chromeApi; this.scheduler = scheduler; this.changed = changed;
    this.tabs = new Map(); this.mode = null; this.paused = false; this.generation = 0;
    this.dropTotals = { expired: 0, rejected: 0 }; this.dropDocuments = new Map();
    this.helper = null; this.started = false; this.queue = Promise.resolve();
  }
  serialize(work) {
    const result = this.queue.then(work); this.queue = result.catch(() => undefined); return result;
  }
  async bounded(work, ms = 1000) {
    let timer;
    try { return await Promise.race([work, new Promise((resolve) => { timer = this.scheduler.setTimeout(() => resolve(null), ms); })]); }
    finally { if (timer !== undefined) this.scheduler.clearTimeout(timer); }
  }
  start() {
    if (this.started) return; this.started = true;
    this.chrome.tabs?.onCreated?.addListener((tab) => {
      if (ownedUrl(tab.url)) { this.tabs.set(tab.id, { ...tab, status: null, document_id: null }); this.changed(); }
    });
    this.chrome.tabs?.onUpdated?.addListener((id, change, tab) => {
      const prior = this.tabs.get(id);
      if (!prior && !ownedUrl(tab?.url ?? change.url)) return;
      if (change.url && !ownedUrl(change.url)) { this.tabs.delete(id); this.changed(); return; }
      const next = { ...prior, ...tab, id };
      if (change.status === 'loading' || change.url) { next.status = null; next.document_id = null; }
      this.tabs.set(id, next);
      if (this.helper?.tab_id === id && change.url && change.url !== this.helper.initial_url) {
        this.helper.touched = true; void this.persistHelper();
      }
      this.changed();
      if (change.status === 'complete' && this.mode && !this.paused) {
        const generation = this.generation;
        void this.serialize(() => this.attach(id, generation)).catch(() => undefined);
      }
    });
    this.chrome.tabs?.onRemoved?.addListener((id) => {
      this.tabs.delete(id);
      if (this.helper?.tab_id === id) { this.helper = { ...this.helper, tab_id: null, closed: true }; void this.persistHelper(); }
      this.changed();
    });
    this.chrome.tabs?.onActivated?.addListener(({ tabId }) => {
      if (this.helper?.tab_id === tabId) { this.helper.touched = true; void this.persistHelper(); }
    });
    this.chrome.tabs?.onReplaced?.addListener((added, removed) => {
      this.tabs.delete(removed);
      if (this.helper?.tab_id === removed) { this.helper = { ...this.helper, tab_id: null, closed: true }; void this.persistHelper(); }
      this.changed();
    });
  }
  async persistHelper() { await this.chrome.storage.local.set({ [OBSERVER_HELPER_KEY]: this.helper }); }
  async discover() {
    const tabs = await this.chrome.tabs.query({ url: [ONLYFANS] }).catch(() => []);
    this.tabs = new Map(tabs.map((tab) => [tab.id, { ...this.tabs.get(tab.id), ...tab }]));
    if (!this.helper) {
      const stored = await this.chrome.storage.local.get([OBSERVER_HELPER_KEY]);
      const candidate = stored?.[OBSERVER_HELPER_KEY];
      if (candidate?.version === 1 && candidate.initial_url === HELPER_URL && typeof candidate.closed === 'boolean') {
        this.helper = candidate;
        // Restored metadata alone never gives permission to navigate or close a tab.
        if (candidate.tab_id !== null && !this.tabs.has(candidate.tab_id)) {
          this.helper = { ...candidate, tab_id: null, closed: true }; await this.persistHelper();
        }
      }
    }
  }
  invalidate() {
    ++this.generation;
    for (const tab of this.tabs.values()) {
      tab.status = null;
      try { void this.chrome.tabs.sendMessage(tab.id, { type: PAGE_CONTROL_MESSAGE_TYPE, version: PAGE_CONTROL_VERSION, action: 'pause' }, { frameId: 0 }).catch(() => undefined); } catch {}
    }
    this.changed();
  }
  configure(mode, paused = false) {
    this.start(); const generation = ++this.generation; this.mode = mode; this.paused = paused;
    return this.serialize(async () => {
      await this.discover();
      if (generation !== this.generation) return;
      await Promise.all([...this.tabs.keys()].map((id) => this.attach(id, generation)));
      if (generation !== this.generation) return;
      if (mode && !paused && ![...this.tabs.values()].some((tab) => tab.status?.active && tab.status.ws2_socket_open)
        && !this.helper?.closed) await this.openHelper(generation);
      this.changed();
    });
  }
  async attach(id, generation) {
    const tab = this.tabs.get(id);
    if (!tab || generation !== this.generation) return;
    const mode = this.mode;
    if (!mode || this.paused) {
      tab.status = null;
      try {
        const reply = await this.bounded(this.chrome.tabs.sendMessage(id, { type: PAGE_CONTROL_MESSAGE_TYPE,
          version: PAGE_CONTROL_VERSION, action: mode ? 'pause' : 'stop' }, { frameId: 0 }));
        if (mode && reply?.ok !== true) await this.bounded(this.chrome.tabs.sendMessage(id, { type: PAGE_CONTROL_MESSAGE_TYPE,
          version: PAGE_CONTROL_VERSION, action: 'stop' }, { frameId: 0 }));
      } catch {
        if (mode) try { await this.bounded(this.chrome.tabs.sendMessage(id, { type: PAGE_CONTROL_MESSAGE_TYPE,
          version: PAGE_CONTROL_VERSION, action: 'stop' }, { frameId: 0 })); } catch {}
      }
      return;
    }
    if (tab.discarded || tab.frozen === true) { tab.status = null; return; }
    try {
      // MAIN is configured before the isolated bridge resumes. The latter checks
      // the committed consent epoch and discards its older delivery queue.
      await this.bounded(this.chrome.scripting.executeScript({ target: { tabId: id, frameIds: [0] },
        world: 'MAIN', files: [`page-hook-mode-${mode}.js`, 'page-hook.js'], injectImmediately: true }));
      if (generation !== this.generation) return;
      const documents = await this.bounded(this.chrome.scripting.executeScript({ target: { tabId: id, frameIds: [0] },
        world: 'ISOLATED', files: ['content.js'], injectImmediately: true }));
      if (generation !== this.generation) return;
      const document = documents?.find((value) => value.frameId === 0);
      tab.document_id = document?.documentId ?? null;
      const status = await this.bounded(this.chrome.tabs.sendMessage(id, { type: PAGE_CONTROL_MESSAGE_TYPE,
        version: PAGE_CONTROL_VERSION, action: 'status' }, document?.documentId ? { documentId: document.documentId } : { frameId: 0 }));
      if (generation !== this.generation || this.tabs.get(id) !== tab) return;
      tab.status = validStatus(status) && status.mode === mode ? status : null;
      await this.refreshDrops(id, generation);
    } catch { if (generation === this.generation) tab.status = null; }
    this.changed();
  }
  async refreshDrops(id, generation = this.generation) {
    const tab = this.tabs.get(id); if (!tab) return;
    try {
      const drops = await this.bounded(this.chrome.tabs.sendMessage(id, { type: 'ofca.capture.queue.status' },
        tab.document_id ? { documentId: tab.document_id } : { frameId: 0 }));
      if (generation !== this.generation || this.tabs.get(id) !== tab || typeof drops?.document !== 'string' || drops.document.length >= 128
        || !['expired', 'rejected'].every((key) => Number.isSafeInteger(drops[key]) && drops[key] >= 0)) return;
      const saved = this.dropDocuments.get(id);
      const previous = saved?.document === drops.document ? saved : { expired: 0, rejected: 0 };
      for (const key of ['expired', 'rejected']) this.dropTotals[key] += Math.max(0, drops[key] - previous[key]);
      this.dropDocuments.set(id, drops); this.changed();
    } catch {}
  }
  observe(message, sender) {
    if (message?.type !== OBSERVER_STATE_TYPE || Object.keys(message).length !== 2 || !validStatus(message.status)
      || sender?.id !== this.chrome.runtime.id || sender.frameId !== 0 || !ownedUrl(sender.url)) return false;
    const tab = this.tabs.get(sender.tab?.id);
    if (!tab?.document_id || sender.documentId !== tab.document_id || message.status.mode !== this.mode) return false;
    tab.status = structuredClone(message.status); this.changed();
    if (this.mode && !this.paused && !this.helper?.closed
      && ![...this.tabs.values()].some((value) => value.status?.active && value.status.ws2_socket_open)) {
      const generation = this.generation;
      void this.serialize(() => this.openHelper(generation)).catch(() => undefined);
    }
    return true;
  }
  async openHelper(generation, explicit = false) {
    if (!this.mode || this.paused || generation !== this.generation || (this.helper?.closed && !explicit)
      || !this.chrome.tabs.create || !this.chrome.tabs.update) return;
    if (this.helper?.tab_id !== null && this.helper?.tab_id !== undefined) return;
    // Content observation has already been registered before initial navigation.
    const tab = await this.chrome.tabs.create({ url: 'about:blank', active: false });
    this.helper = { version: 1, tab_id: tab.id, initial_url: HELPER_URL, closed: false, touched: false };
    await this.persistHelper();
    if (generation !== this.generation) return;
    this.tabs.set(tab.id, { ...tab, url: HELPER_URL, status: null, document_id: null });
    await this.chrome.tabs.update(tab.id, { url: HELPER_URL });
    this.changed();
  }
  reopen() { return this.serialize(async () => {
    if (!this.mode || this.paused || !this.helper?.closed) throw Error('helper_not_ready');
    await this.openHelper(this.generation, true);
    if (this.helper?.tab_id == null || this.helper.closed) throw Error('helper_not_ready');
  }); }
  snapshot() {
    const tabs = [...this.tabs.values()];
    return { tabs, drops: { ...this.dropTotals }, drop_sources: Object.fromEntries(this.dropDocuments), attached: tabs.some((tab) => tab.status?.active === true && tab.status.mode === this.mode),
      helper: this.helper?.closed ? 'closed' : this.helper?.tab_id != null ? 'open' : 'none' };
  }
}
