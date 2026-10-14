import { isParticipant, UUID } from '../../shared/browser-tasks/wire.mjs';
import { NATIVE_LAUNCH_KEY } from './onboarding-native-launch.mjs';
import { BROWSER_TASK_INSTANCE_KEY as INSTANCE, BROWSER_TASK_ADOPTION_KEY as ADOPTION } from './browser-task-entry.mjs';

/** Presentation evidence only. The existing consent and identity owners remain authoritative. */
export function createBrowserTaskOwner({ chromeApi, onboarding, consentController, legalActivationController, identityBridge, openCreatorAccount,
  authentication, now = Date.now }) {
  const epoch = crypto.randomUUID();
  const owners = new Map();
  let queue = Promise.resolve();
  const serial = work => { const next = queue.then(work); queue = next.catch(() => undefined); return next; };
  const ownerKey = task => `${task.task_id}/${task.admission_generation}`;
  const owns = owner => owners.get(ownerKey(owner.payload.task)) === owner;
  let instancePromise, revision = 0, accountGeneration = 0, consentGeneration = 0;
  let previousAccount, previousConsent, previousValue;
  const instance = () => instancePromise ??= (async () => {
    const saved = (await chromeApi.storage.local.get(INSTANCE))[INSTANCE];
    if (saved !== undefined && !UUID.test(saved)) throw Error('unavailable');
    const id = saved ?? crypto.randomUUID();
    if (!saved) await chromeApi.storage.local.set({ [INSTANCE]: id });
    return id;
  })().catch(error => { instancePromise = null; throw error; });
  async function context(payload, initial = false, { authorize = async () => {}, signal } = {}) {
    const saved = (await chromeApi.storage.local.get(INSTANCE))[INSTANCE];
    if (saved !== undefined && !UUID.test(saved)) throw Error('unavailable');
    if (payload.participant && saved !== payload.participant.instance_id) throw Error('profile_mismatch');
    const current = await onboarding.browserTaskContext(payload.task.journey_id, {
      adopt: initial && payload.entry_source === 'desktop' && payload.participant === null
        && payload.creator_account_id === null && payload.task.purpose === 'browser.access',
      authorize,
      canAdopt: () => !signal?.aborted && !authentication?.hasLiveFlow() && [...owners.values()].every(owner =>
        owner.payload.task.expires_at * 1000 <= now() || owner.payload.task.journey_id === payload.task.journey_id),
    });
    const id = await instance();
    const participant = { instance_id: id, draft_scope_id: current.draft_scope.scope_id,
      disclosure_bundle_id: current.draft_scope.disclosure_bundle_id };
    if (!isParticipant(participant)) throw Error('unavailable');
    if (payload.participant && Object.keys(participant).some(key => participant[key] !== payload.participant[key])) throw Error('task_changed');
    if (initial && payload.entry_source === 'extension') {
      const [session, saved] = await Promise.all([chromeApi.storage.session.get(NATIVE_LAUNCH_KEY), chromeApi.storage.local.get(ADOPTION)]);
      const launch = session[NATIVE_LAUNCH_KEY], adoption = saved[ADOPTION];
      const scoped = value => value?.journey_id === payload.task.journey_id
        && value.draft_scope?.scope_id === current.draft_scope.scope_id
        && value.draft_scope?.disclosure_bundle_id === current.draft_scope.disclosure_bundle_id;
      if (!current.record || !(scoped(launch) && launch.phase === 'pending' && launch.expires_at > now()
        || scoped(adoption) && adoption.instance_id === id)) throw Error('task_changed');
    }
    return { ...current, participant };
  }
  return Object.freeze({
    attach(payload, { authorize = async () => {}, signal } = {}) { return serial(async () => {
      const admitted = async () => { if (signal?.aborted) throw Error('task_changed'); await authorize();
        if (signal?.aborted) throw Error('task_changed'); };
      await admitted();
      for (const [key, owner] of owners) if (owner.payload.task.expires_at * 1000 <= now()) owners.delete(key);
      const current = await context(payload, true, { authorize: admitted, signal });
      await admitted();
      if (current.record) await chromeApi.storage.local.set({ [ADOPTION]: { journey_id: payload.task.journey_id,
        instance_id: current.participant.instance_id, draft_scope: current.draft_scope } });
      await admitted();
      const owner = { payload, draft_scope: current.draft_scope, selected: null, adopted: JSON.stringify(current.draft_scope) };
      owners.set(ownerKey(payload.task), owner);
      return owner;
    }); },
    read(owner) { return this.readPaired(owner, null); },
    async readPaired(owner, pinnedAccountId) {
      if (!owns(owner)) throw Error('task_changed');
      const current = await context(owner.payload);
      // A task retains its admitted draft until the workspace has one.
      const scope = current.record ? current.draft_scope : owner.draft_scope;
      const [consent, legal] = await Promise.all([consentController.status(), legalActivationController.status()]);
      const mode = consent.consent?.mode;
      const full = mode === 'full' || mode === 'paused' && consent.consent?.resume_mode === 'full';
      const permitted = legal.configured && !legal.requires_reauthorization && full && consent.onlyfans_permission === true;
      // A paused authenticated channel names its pinned creator without reading
      // OnlyFans or unlocking capture storage. Helpers cannot supply this pin.
      const account = permitted ? mode === 'paused' && pinnedAccountId !== null
        ? pinnedAccountId : await identityBridge.currentAccountId() : null;
      if (account !== null && owner.payload.creator_account_id !== null && account !== owner.payload.creator_account_id) throw Error('account_mismatch');
      const profile = permitted && mode !== 'paused' ? await identityBridge.currentProfile?.() : null;
      if (profile && profile.creator_account_id !== account) throw Error('task_changed');
      if (permitted) {
        const [latest, currentLegal] = await Promise.all([consentController.status(), legalActivationController.status()]);
        if (latest.consent?.consent_epoch !== consent.consent?.consent_epoch || latest.consent?.mode !== mode
          || latest.onlyfans_permission !== true || !currentLegal.configured || currentLegal.requires_reauthorization) throw Error('task_changed');
      }
      const state = !legal.configured ? 'unavailable' : !full || legal.requires_reauthorization ? 'needs-consent'
        : !consent.onlyfans_permission ? 'needs-permission' : !account ? 'needs-account' : mode === 'paused' ? 'paused' : 'ready';
      // Losing/changing a selected identity fences this task. It is never
      // silently repurposed for a different creator or disclosure bundle.
      const selected = JSON.stringify([account, scope]);
      if (owner.selected !== null && owner.selected !== selected) throw Error('task_changed');
      if (account !== null) owner.selected = selected;
      if (current.record && owner.adopted !== JSON.stringify(scope)) {
        await serial(async () => {
          if (!owns(owner)) throw Error('task_changed');
          await chromeApi.storage.local.set({ [ADOPTION]: { journey_id: owner.payload.task.journey_id,
            instance_id: current.participant.instance_id, draft_scope: scope } });
          owner.adopted = JSON.stringify(scope);
        });
      }
      if (previousAccount !== account) { accountGeneration++; previousAccount = account; }
      if (previousConsent !== consent.consent?.consent_epoch) { consentGeneration++; previousConsent = consent.consent?.consent_epoch; }
      const value = { profile: 'browser-task-observation.v1', task_id: owner.payload.task.task_id,
        journey_id: owner.payload.task.journey_id, admission_generation: owner.payload.task.admission_generation,
        extension_id: chromeApi.runtime.id, instance_id: current.participant.instance_id, epoch,
        account_generation: accountGeneration, consent_generation: consentGeneration,
        draft_scope_id: scope.scope_id, disclosure_bundle_id: scope.disclosure_bundle_id, state, creator_account_id: account,
        ...(profile?.display_name ? { creator_display_name: profile.display_name } : {}),
        ...(profile?.username ? { creator_username: profile.username } : {}) };
      const encoded = JSON.stringify(value);
      if (encoded !== previousValue) { revision++; previousValue = encoded; }
      if (!owns(owner)) throw Error('task_changed');
      return { ...value, revision };
    },
    async open(owner, current) {
      await current();
      await context(owner.payload);
      if (!['browser.access', 'browser.identity'].includes(owner.payload.task.purpose)) throw Error('unavailable');
      const state = await this.read(owner);
      await current();
      if (state.state === 'needs-account') return openCreatorAccount();
      return onboarding.openBrowserTask(owner.payload.task, owner.draft_scope, current);
    },
    release(owner) { return serial(async () => {
      if (!owner || !owns(owner)) return;
      owners.delete(ownerKey(owner.payload.task));
      await onboarding.closeBrowserTask?.(owner.payload.task);
    }); },
  });
}

/** Install arrival refreshes only the exact still-open helper document. */
export async function attachInstalledHelpers(chromeApi) {
  const { helperGeneration } = await import('../../shared/browser-tasks/wire.mjs');
  const candidates = (await chromeApi.tabs.query({ url: 'http://bridge.localhost:17871/browser-tasks*' }))
    .filter(tab => helperGeneration(tab.url));
  for (const tab of candidates) {
    await chromeApi.scripting.executeScript({ target: { tabId: tab.id, frameIds: [0] }, world: 'ISOLATED', args: [tab.url],
      func: expected => { if (window === window.top && location.href === expected) location.reload(); } }).catch(() => undefined);
  }
  return candidates.length > 0;
}
