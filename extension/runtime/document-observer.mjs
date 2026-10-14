import { PAGE_CONTROL_MESSAGE_TYPE, PAGE_CONTROL_VERSION } from '../capture/envelopes.mjs';

export const OBSERVER_STATE_TYPE = 'ofca.observer.state';
export const OBSERVER_HELPER_KEY = 'onboarding_observer_helper_v2';
const HELPER_SESSION_KEY = 'onboarding_observer_session_v2';
export const OBSERVER_REOPEN_TYPE = 'ofca.ui.reopen-background-tab';
const ONLYFANS = 'https://onlyfans.com/*';
const HELPER_URL = 'https://onlyfans.com/';
export const SIGNING_HELPER_PAGE = 'signing-helper.html';
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
    this.ownerSession = null; this.helperSuspended = false;
    this.helperEpoch = 0; this.helperPersistenceSuspended = false; this.helperWrites = new Set();
    this.helperWriteTail = Promise.resolve();
    this.signingCreation = null; this.retiringHelper = null;
    this.signingAbort = new AbortController();
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
    this.chrome.tabs?.onRemoved?.addListener((id, info = {}) => {
      this.tabs.delete(id);
      if (this.helper?.tab_id === id) {
        const windowClosing = info.isWindowClosing === true;
        const retired = this.retiringHelper === id;
        if (!retired) this.cancelSigning();
        this.helperSuspended = windowClosing;
        this.helper = { ...this.helper, tab_id: null, closed: !windowClosing && !retired,
          closure: windowClosing || retired ? 'lifecycle' : 'explicit' };
        void this.persistHelper();
      }
      this.changed();
    });
    this.chrome.tabs?.onActivated?.addListener(({ tabId }) => {
      if (this.helper?.tab_id === tabId) {
        this.cancelSigning(); this.helper.touched = true; void this.persistHelper();
      }
    });
    this.chrome.tabs?.onReplaced?.addListener((added, removed) => {
      this.tabs.delete(removed);
      if (this.helper?.tab_id === removed) {
        this.cancelSigning();
        this.helper = { ...this.helper, tab_id: null, closed: false, closure: 'lifecycle' };
        void this.persistHelper();
      }
      // A replacement is not a stop request and is not automatically our tab.
      // Discover/attach it before considering any new helper navigation.
      const generation = this.generation;
      void this.serialize(async () => {
        await this.discover();
        if (generation === this.generation && this.tabs.has(added)) await this.attach(added, generation);
      }).catch(() => undefined);
      this.changed();
    });
  }
  persistHelper(epoch = this.helperEpoch) {
    if (this.helperPersistenceSuspended || epoch !== this.helperEpoch) return Promise.resolve();
    const snapshot = { [OBSERVER_HELPER_KEY]: structuredClone(this.helper) };
    const write = this.helperWriteTail.then(() => this.chrome.storage.local.set(snapshot));
    this.helperWriteTail = write.catch(() => undefined);
    this.helperWrites.add(write);
    void write.finally(() => this.helperWrites.delete(write)).catch(() => undefined);
    return write;
  }
  async discover() {
    const epoch = this.helperEpoch;
    const tabs = await this.chrome.tabs.query({ url: [ONLYFANS] }).catch(() => []);
    if (epoch !== this.helperEpoch) return;
    this.tabs = new Map(tabs.map((tab) => [tab.id, { ...this.tabs.get(tab.id), ...tab }]));
    if (this.helperPersistenceSuspended) return;
    if (!this.ownerSession) {
      const session = this.chrome.storage.session;
      const stored = session ? await session.get([HELPER_SESSION_KEY]) : {};
      if (epoch !== this.helperEpoch) return;
      this.ownerSession = typeof stored[HELPER_SESSION_KEY] === 'string' ? stored[HELPER_SESSION_KEY] : crypto.randomUUID();
      if (session && !stored[HELPER_SESSION_KEY]) await session.set({ [HELPER_SESSION_KEY]: this.ownerSession });
      if (epoch !== this.helperEpoch) return;
    }
    if (!this.helper) {
      const stored = await this.chrome.storage.local.get([OBSERVER_HELPER_KEY]);
      if (epoch !== this.helperEpoch) return;
      const candidate = stored?.[OBSERVER_HELPER_KEY];
      if (candidate?.version === 2 && candidate.initial_url === HELPER_URL
        && typeof candidate.closed === 'boolean'
        && (candidate.tab_id === null || Number.isSafeInteger(candidate.tab_id) && candidate.tab_id >= 0)) {
        const owned = candidate.owner_session === this.ownerSession
          && (this.tabs.has(candidate.tab_id) || await this.isPackagedHelper(candidate.tab_id));
        if (epoch !== this.helperEpoch) return;
        this.helper = { version: 2, initial_url: HELPER_URL, owner_session: this.ownerSession,
          tab_id: owned ? candidate.tab_id : null, closed: candidate.closed,
          closure: candidate.closed ? 'explicit' : owned ? 'none' : 'lifecycle',
          touched: !owned || candidate.touched === true };
        await this.persistHelper(epoch);
      }
    } else if (this.helper.tab_id !== null && !this.tabs.has(this.helper.tab_id)
      && !await this.isPackagedHelper(this.helper.tab_id)) {
      if (epoch !== this.helperEpoch) return;
      // Absence after a restart is not evidence of a deliberate helper close.
      this.helper = { ...this.helper, tab_id: null, closed: false, closure: 'lifecycle' };
      await this.persistHelper(epoch);
    }
  }

  async isPackagedHelper(id) {
    if (!Number.isInteger(id) || !this.chrome.tabs.get || !this.chrome.runtime.getURL) return false;
    try {
      const tab = await this.chrome.tabs.get(id);
      return tab.url === this.chrome.runtime.getURL(SIGNING_HELPER_PAGE);
    } catch { return false; }
  }

  async resetForDeletion() {
    this.helperPersistenceSuspended = true; ++this.helperEpoch;
    this.mode = null; this.paused = false;
    this.helper = null; this.ownerSession = null; this.helperSuspended = false;
    this.dropTotals = { expired: 0, rejected: 0 }; this.dropDocuments.clear();
    this.invalidate();
    await this.queue;
    // Lifecycle writes run outside attachment work and must settle before storage is cleared.
    const writes = await Promise.allSettled([...this.helperWrites]);
    if (writes.some((write) => write.status === 'rejected')) throw Error('observer_persistence_failed');
  }

  invalidate() {
    ++this.generation;
    this.cancelSigning();
    for (const tab of this.tabs.values()) {
      tab.status = null;
      try { void this.chrome.tabs.sendMessage(tab.id, { type: PAGE_CONTROL_MESSAGE_TYPE, version: PAGE_CONTROL_VERSION, action: 'pause' }, { frameId: 0 }).catch(() => undefined); } catch {}
    }
    this.changed();
  }
  configure(mode, paused = false) {
    this.start(); const generation = ++this.generation; this.mode = mode; this.paused = paused;
    this.cancelSigning();
    if (mode) this.helperPersistenceSuspended = false;
    this.helperSuspended = false;
    return this.serialize(async () => {
      await this.discover();
      if (generation !== this.generation) return;
      await Promise.all([...this.tabs.keys()].map((id) => this.attach(id, generation)));
      if (generation !== this.generation) return;
      if (mode && !paused && this.tabs.size === 0
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
    // A closed page socket is a status, not a reason to open another platform
    // tab. The existing document and the bounded signer own their recovery.
    return true;
  }
  async openHelper(generation, explicit = false) {
    if (!this.mode || this.paused || this.helperSuspended || generation !== this.generation || (this.helper?.closed && !explicit)
      || this.signingCreation !== null || !this.chrome.tabs.create || !this.chrome.tabs.update) return;
    if (this.helper?.tab_id !== null && this.helper?.tab_id !== undefined) return;
    // Content observation has already been registered before initial navigation.
    const epoch = this.helperEpoch;
    const tab = await this.chrome.tabs.create({ url: 'about:blank', active: false });
    if (epoch !== this.helperEpoch || generation !== this.generation) return;
    this.helper = { version: 2, tab_id: tab.id, initial_url: HELPER_URL, closed: false, touched: false,
      closure: 'none', owner_session: this.ownerSession };
    await this.persistHelper(epoch);
    if (generation !== this.generation) return;
    this.tabs.set(tab.id, { ...tab, url: HELPER_URL, status: null, document_id: null });
    await this.chrome.tabs.update(tab.id, { url: HELPER_URL });
    this.changed();
  }

  cancelSigning() {
    this.signingAbort.abort(Object.assign(new Error('Observer operation changed'), { code: 'observer_changed' }));
    this.signingAbort = new AbortController();
  }

  recoverSigning({ signal: requestSignal, reserve, readExisting, createHelper, readHelper }) {
    const signal = requestSignal ? AbortSignal.any([requestSignal, this.signingAbort.signal]) : this.signingAbort.signal;
    const generation = this.generation;
    const epoch = this.helperEpoch;
    const unavailable = () => Object.assign(new Error('History helper is unavailable'), { code: 'history_unavailable' });
    const assertCurrent = () => {
      signal?.throwIfAborted();
      if (generation !== this.generation || epoch !== this.helperEpoch || this.mode !== 'full'
        || this.paused || this.helperPersistenceSuspended || this.helperSuspended || this.helper?.closed) throw unavailable();
    };
    return this.serialize(async () => {
      assertCurrent();
      if (this.signingCreation !== null) throw unavailable();
      const permitted = await this.chrome.permissions.contains({
        origins: [ONLYFANS], permissions: ['webRequest'],
      });
      assertCurrent();
      if (!permitted) throw unavailable();
      await this.discover();
      assertCurrent();
      const oldId = this.helper?.tab_id;
      if (Number.isInteger(oldId)) {
        if (!await this.isPackagedHelper(oldId)) {
          try {
            const result = await readExisting(oldId, signal);
            assertCurrent();
            return result;
          }
          catch (error) {
            assertCurrent();
            if (error?.code !== 'browser_unavailable') throw error;
          }
        }
        assertCurrent();
        // An activated or navigated helper is now a user tab. Preserve it and
        // refuse automatic replacement rather than accumulate another helper.
        const oldTab = await this.chrome.tabs.get(oldId);
        assertCurrent();
        const helperUrl = this.chrome.runtime.getURL(SIGNING_HELPER_PAGE);
        if (this.helper?.tab_id !== oldId || this.helper.touched || oldTab.active !== false
          || oldTab.pendingUrl || ![HELPER_URL, helperUrl].includes(oldTab.url)) throw unavailable();
      }
      await reserve(signal);
      assertCurrent();
      if (Number.isInteger(oldId)) {
        if (this.helper?.tab_id !== oldId || this.helper.touched) throw unavailable();
        this.retiringHelper = oldId;
        try { await this.chrome.tabs.remove(oldId); }
        finally { this.retiringHelper = null; }
        assertCurrent();
        this.helper = { ...this.helper, tab_id: null, closed: false, closure: 'lifecycle' };
        await this.persistHelper(epoch);
        assertCurrent();
      }
      const host = await createHelper({
        signal,
        helperPagePath: SIGNING_HELPER_PAGE,
        trackCreation: (create) => {
          assertCurrent();
          const pending = Promise.resolve().then(() => { assertCurrent(); return create(); });
          this.signingCreation = pending;
          return pending.catch((error) => {
            if (this.signingCreation === pending) this.signingCreation = null;
            throw error;
          });
        },
        onCreated: async (id) => {
          this.signingCreation = null;
          // A late acknowledgement still records ownership, but deletion must
          // never recreate storage. Only an untouched packaged page may be closed.
          if (epoch !== this.helperEpoch || this.helperPersistenceSuspended) {
            const tab = await this.chrome.tabs.get(id).catch(() => null);
            if (tab?.active === false && tab.url === this.chrome.runtime.getURL(SIGNING_HELPER_PAGE)
              && !tab.pendingUrl) await this.chrome.tabs.remove(id);
            throw unavailable();
          }
          this.helper = { version: 2, tab_id: id, initial_url: HELPER_URL,
            closed: false, touched: false, closure: 'none', owner_session: this.ownerSession };
          await this.persistHelper(epoch);
          this.changed();
          assertCurrent();
        },
      });
      assertCurrent();
      const result = await readHelper(host, signal);
      assertCurrent();
      return result;
    });
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
