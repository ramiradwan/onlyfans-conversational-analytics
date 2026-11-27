const dashboardSections = ['dashboard-overview', 'dashboard-basis', 'dashboard-notice', 'dashboard-recent', 'dashboard-setup'];

function checkDashboardChange(previous, frame, issues) {
  const oldTask = JSON.parse(previous.task.slice('dashboard:'.length));
  const newTask = JSON.parse(frame.task.slice('dashboard:'.length));
  const shifted = new Set();
  let offset = 0;
  let overviewShift = 0;
  const check = (id, matches, required = true) => {
    const before = previous.regions.find(region => region.id === id)?.rect;
    const after = frame.regions.find(region => region.id === id)?.rect;
    if (!required && !before && !after) return;
    if (!before || !after || !matches(before, after)) issues.add(`${id}: moved across task change`);
  };
  for (const id of ['dashboard-title', 'freshness-row', 'freshness-status']) {
    check(id, (before, after) => before.every((value, index) => value === after[index]), id === 'dashboard-title');
  }
  dashboardSections.forEach((id, index) => {
    const changed = oldTask[index] !== newTask[index];
    if (changed || offset) shifted.add(id);
    check(id, (before, after) => {
      const growth = after[3] - before[3];
      const matches = before[0] === after[0] && before[1] + offset === after[1]
        && before[2] === after[2] && (changed || growth === 0);
      if (index === 0 && changed) overviewShift = growth;
      if (changed) offset += growth;
      return matches;
    });
  });
  const oldOverview = oldTask[0].split(':')[0];
  const newOverview = newTask[0].split(':')[0];
  const conversations = frame.regions.find(region => region.id === 'metric-conversations');
  const messages = frame.regions.find(region => region.id === 'metric-messages');
  const narrow = conversations && messages && conversations.rect[0] === messages.rect[0];
  for (const id of ['metric-conversations', 'metric-messages', 'metric-Received', 'metric-Sent']) {
    if (oldOverview !== newOverview) {
      shifted.add(id);
      if (previous.regions.some(region => region.id === id) !== (oldOverview === 'true')
        || frame.regions.some(region => region.id === id) !== (newOverview === 'true')) {
        issues.add(`${id}: missing or present outside its overview`);
      }
    } else if (newOverview === 'true') {
      const delta = narrow && id !== 'metric-conversations' ? overviewShift : 0;
      if (delta) shifted.add(id);
      check(id, (before, after) => before[1] + delta === after[1]
        && [0, 2, 3].every(index => before[index] === after[index]));
    }
  }
  return { shifted, sources: source => shifted.has(source.region) };
}

function checkPasskeyChange(previous, frame, issues) {
  const sameFamily = previous.task.split(':')[1] === frame.task.split(':')[1];
  const before = Object.fromEntries(previous.regions.map(region => [region.id, region.rect]));
  const after = Object.fromEntries(frame.regions.map(region => [region.id, region.rect]));
  for (const [id, fixed] of [['passkey-card', [0, 1, 2]], ['passkey-heading', sameFamily ? [0, 1, 2, 3] : [0, 1, 2]],
    ['passkey-feedback', sameFamily ? [0, 1, 2] : [0, 2]]]) {
    if (!before[id] || !after[id] || fixed.some(index => before[id][index] !== after[id][index])) {
      issues.add(`${id}: moved across task change`);
    }
  }
  if (before['passkey-card'] && after['passkey-card'] && before['passkey-feedback'] && after['passkey-feedback']) {
    const trailing = (card, feedback) => card[1] + card[3] - feedback[1] - feedback[3];
    if (trailing(before['passkey-card'], before['passkey-feedback']) !== trailing(after['passkey-card'], after['passkey-feedback'])) {
      issues.add('passkey-feedback: moved across task change');
    }
  }
  const shifted = new Set(['passkey-card', 'passkey-feedback']);
  if (!sameFamily) shifted.add('passkey-heading');
  let footerMoved = false;
  for (const region of frame.regions.filter(region => region.flowY !== undefined)) {
    const prior = before[region.id];
    if (!prior || region.rect[1] !== region.flowY || [0, 2, 3].some(index => prior[index] !== region.rect[index])) {
      issues.add(`${region.id}: moved across task change`);
    } else if (prior[1] !== region.rect[1]) {
      shifted.add(region.id);
      footerMoved = true;
    }
  }
  return { shifted, sources: source => source.region === 'passkey-feedback'
    || (!sameFamily && source.region === 'passkey-card') || (footerMoved && source.tag === 'FOOTER' && !source.region) };
}

export function inspectFrames(frames) {
  const baseline = new Map();
  const overlays = new Set();
  const issues = new Set();
  const taskTransitions = [];
  let previous;
  for (const frame of frames) {
    if (previous && typeof frame.task === 'string' && typeof previous.task === 'string' && frame.task !== previous.task) {
      let change;
      if (previous.task.startsWith('passkey:') && frame.task.startsWith('passkey:')) {
        change = checkPasskeyChange(previous, frame, issues);
      } else if (previous.task.startsWith('dashboard:') && frame.task.startsWith('dashboard:')) {
        change = checkDashboardChange(previous, frame, issues);
      }
      if (change) {
        for (const id of change.shifted) { baseline.delete(id); overlays.delete(id); }
      } else {
        baseline.clear();
        overlays.clear();
      }
      taskTransitions.push({ start: previous.nativeAt ?? previous.at,
        end: frame.nativePaintThrough ?? frame.nativeAt ?? frame.paintThrough ?? frame.at, sources: change?.sources });
    }
    for (const issue of frame.errors ?? []) issues.add(issue);
    const present = new Set();
    for (const region of frame.regions) {
      if (region.overlay) overlays.add(region.id);
      present.add(region.id);
      if (region.overflow) issues.add(`${region.id}: overflow`);
      const before = baseline.get(region.id);
      if (before && !region.readingChild && region.rect.some((value, index) => value !== before[index])) issues.add(`${region.id}: moved or resized`);
      if (!before && !region.readingChild) baseline.set(region.id, region.rect);
    }
    for (const id of baseline.keys()) if (!present.has(id)) {
      if (overlays.has(id)) baseline.delete(id);
      else issues.add(`${id}: removed`);
    }
    if (frame.shifts?.some(entry => entry.value > 0 && !taskTransitions.some(({ start, end, sources }) =>
      entry.at >= start && entry.at <= end && (!sources || entry.sources?.length && entry.sources.every(sources))))) {
      issues.add('layout shift after first paint');
    }
    previous = frame;
  }
  return [...issues];
}

export function observeRegions({ requiredRegions = [], taskTransitions = false, nativeTimeline = false } = {}) {
  // Keep the browser clock and rendering scheduler before Playwright replaces
  // them. Event.timeStamp is replaced by its clock too; it is not a fallback.
  const nativeNow = performance.now.bind(performance);
  const nativeFrame = requestAnimationFrame.bind(window);
  const nativeCancelFrame = cancelAnimationFrame.bind(window);
  const nativeTask = setTimeout.bind(window);
  const capturedAt = nativeNow();
  const clockVerified = [performance.now, requestAnimationFrame, setTimeout]
    .every(method => Function.prototype.toString.call(method).includes('[native code]'));
  const state = { frames: [], startedAt: null, firstPaint: null, shifts: [], rawShifts: [], signature: '', errors: [],
    nativeTimeline: nativeTimeline ? { basis: 'pre-clock-performance/v1', capturedAt, timeOrigin: performance.timeOrigin,
      clockVerified, endAt: null, completed: false } : null };
  window.__regionWatcher = state;
  if (nativeTimeline && !clockVerified) state.errors.push('Native observer clock was not captured before clock mocking');
  const anchors = new Map();
  if (!PerformanceObserver.supportedEntryTypes.includes('layout-shift')) state.errors.push('Layout shift observer unavailable');
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) if (entry.name === 'first-paint') state.firstPaint = entry.startTime;
  }).observe({ type: 'paint', buffered: true });
  const recordShifts = entries => {
    for (const entry of entries) {
      const record = { sequence: state.rawShifts.length + 1, at: entry.startTime, deliveredAt: nativeNow(), value: entry.value, hadRecentInput: entry.hadRecentInput,
      sources: (entry.sources ?? []).map(({ node, previousRect, currentRect }) => ({ tag: node?.nodeName, region: node?.closest?.('[data-reserved-region]')?.dataset.reservedRegion ?? null,
        from: [previousRect.x, previousRect.y, previousRect.width, previousRect.height], to: [currentRect.x, currentRect.y, currentRect.width, currentRect.height] })) };
      state.rawShifts.push(record);
      state.shifts.push(record);
    }
  };
  const shiftObserver = new PerformanceObserver(list => recordShifts(list.getEntries()));
  shiftObserver.observe({ type: 'layout-shift', buffered: true });
  const visible = (node) => {
    const css = getComputedStyle(node);
    const screenReaderOnly = css.position === 'absolute' && css.width === '1px' && css.height === '1px' && (css.clip !== 'auto' || css.clipPath !== 'none');
    let transparent = false;
    for (let parent = node; parent; parent = parent.parentElement) transparent ||= getComputedStyle(parent).opacity === '0';
    return node.getClientRects().length && css.visibility !== 'hidden' && !screenReaderOnly && !transparent;
  };
  const beyond = (rect, outer) => rect.left < outer.left - 0.01 || rect.right > outer.right + 0.01 || rect.top < outer.top - 0.01 || rect.bottom > outer.bottom + 0.01;
  function paintedBox(element) {
    const rect = element.getBoundingClientRect();
    if (element.textContent.trim() || element.matches('svg, svg *, button, input, a, textarea, select')) return rect;
    let { left, right, top, bottom } = rect;
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      const css = getComputedStyle(parent), bounds = parent.getBoundingClientRect();
      if (/(hidden|clip)/u.test(css.overflowX)) { left = Math.max(left, bounds.left); right = Math.min(right, bounds.right); }
      if (/(hidden|clip)/u.test(css.overflowY)) { top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom); }
    }
    return { left, right, top, bottom };
  }
  function layoutPosition(node, rect) {
    let x = rect.x, y = rect.y, fixed = false;
    for (let parent = node; parent; parent = parent.parentElement) {
      const css = getComputedStyle(parent);
      fixed ||= css.position === 'fixed';
      if (parent.dataset.regionRole === 'overlay' && css.transform !== 'none') { const matrix = new DOMMatrix(css.transform); x -= matrix.m41; y -= matrix.m42; }
      if (parent !== node && parent !== document.body && parent !== document.documentElement) { x += parent.scrollLeft; y += parent.scrollTop; }
    }
    return [x + (fixed ? 0 : scrollX), y + (fixed ? 0 : scrollY), rect.width, rect.height];
  }
  function sample() {
    const regions = [];
    const ids = new Set();
    if (taskTransitions === 'dashboard') {
      const heading = document.querySelector('[data-dashboard-heading]');
      if (heading && visible(heading)) regions.push({ id: 'dashboard-title', rect: layoutPosition(heading, heading.getBoundingClientRect()) });
    } else if (taskTransitions === 'passkey') {
      const heading = document.querySelector('[data-passkey-task] main h1');
      if (heading && visible(heading)) regions.push({ id: 'passkey-heading', rect: layoutPosition(heading, heading.getBoundingClientRect()) });
    }
    for (const node of document.querySelectorAll('[data-reserved-region]')) {
      if (!node.getClientRects().length) continue;
      const id = node.dataset.reservedRegion;
      const rect = node.getBoundingClientRect();
      const reading = node.dataset.regionRole === 'scroll';
      let overflow = ids.has(id);
      const overflowDetails = [];
      const record = (element, reason, bounds = element.getBoundingClientRect()) => {
        overflow = true;
        overflowDetails.push({ node: element.tagName, reason, rect: [bounds.x, bounds.y, bounds.width, bounds.height] });
      };
      ids.add(id);
      if (visible(node)) {
        if (reading && node.scrollWidth > node.clientWidth) overflow = true;
        const readers = [reading ? node : null, ...node.querySelectorAll('[data-reading-viewport]')].filter((reader) => reader && reader.closest('[data-reserved-region]') === node);
        for (const reader of readers) {
          const readerRect = reader.getBoundingClientRect();
          if (reader.scrollWidth > reader.clientWidth) record(reader, 'horizontal reading escape');
          const walker = document.createTreeWalker(reader, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const text = walker.currentNode;
            if (!text.textContent.trim() || !visible(text.parentElement)) continue;
            if (text.parentElement.closest('[data-reading-viewport], [data-region-role="scroll"]') !== reader) continue;
            const range = document.createRange(); range.selectNodeContents(text);
            for (const box of range.getClientRects()) {
              for (let ancestor = text.parentElement; ancestor && ancestor !== reader; ancestor = ancestor.parentElement) {
                const css = getComputedStyle(ancestor), bounds = ancestor.getBoundingClientRect();
                if ((/(hidden|clip)/u.test(css.overflowX) && (box.left < bounds.left || box.right > bounds.right))
                  || (/(hidden|clip)/u.test(css.overflowY) && (box.top < bounds.top || box.bottom > bounds.bottom))) record(ancestor, 'clipped reading text', box);
              }
            }
          }
          for (const control of reader.querySelectorAll('button, a[href], input, select, textarea')) {
            if (!visible(control)) continue;
            if (control.closest('[data-reading-viewport], [data-region-role="scroll"]') !== reader) continue;
            const bounds = control.getBoundingClientRect();
            if (bounds.top + reader.scrollTop < readerRect.top || bounds.left + reader.scrollLeft < readerRect.left) record(control, 'unreachable reading action');
          }
          const controls = [...reader.querySelectorAll('button, a[href], input, select, textarea')].filter((control) => visible(control) && control.closest('[data-reading-viewport], [data-region-role="scroll"]') === reader);
          for (let index = 0; index < controls.length; index++) for (const other of controls.slice(index + 1)) {
            if (controls[index].contains(other) || other.contains(controls[index])) continue;
            const first = controls[index].getBoundingClientRect(), second = other.getBoundingClientRect();
            if (Math.min(first.right, second.right) > Math.max(first.left, second.left) && Math.min(first.bottom, second.bottom) > Math.max(first.top, second.top)) record(other, 'overlapping reading actions');
          }
        }
        const content = [...node.querySelectorAll('[data-region-content]')].filter((child) => child.closest('[data-reserved-region]') === node);
        for (const child of content) {
          if (!visible(child)) continue;
          if (reading) {
            if (!/(auto|scroll)/u.test(getComputedStyle(node).overflowY) && !node.querySelector('[data-reading-viewport]')) overflow = true;
            const bounds = child.getBoundingClientRect();
            if (bounds.left < rect.left || bounds.right > rect.right) record(child, 'horizontal reading escape');
            continue;
          }
          if (beyond(child.getBoundingClientRect(), rect)) record(child, 'content box');
          for (const descendant of child.querySelectorAll('*')) {
            if (!visible(descendant) || descendant.closest('[aria-hidden="true"]')?.style.visibility === 'hidden') continue;
            const reader = descendant.closest('[data-region-role="scroll"], [data-reading-viewport]');
            if (reader && reader !== node && node.contains(reader)) {
              if (reader.scrollWidth > reader.clientWidth) overflow = true;
              continue;
            }
            const css = getComputedStyle(descendant);
            if (beyond(paintedBox(descendant), rect)) record(descendant, 'descendant box');
            if ((css.textOverflow === 'ellipsis' || css.webkitLineClamp !== 'none') && descendant.scrollWidth > descendant.clientWidth) record(descendant, 'clipped text');
          }
          const walker = document.createTreeWalker(child, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const text = walker.currentNode;
            if (!text.textContent.trim() || !visible(text.parentElement)) continue;
            const readingViewport = text.parentElement.closest('[data-region-role="scroll"]');
            if (readingViewport && readingViewport !== node && node.contains(readingViewport)) continue;
            const range = document.createRange(); range.selectNodeContents(text);
            for (const box of range.getClientRects()) {
              if (beyond(box, rect)) record(text.parentElement, 'text box', box);
              for (let ancestor = text.parentElement; ancestor && ancestor !== node; ancestor = ancestor.parentElement) {
                const css = getComputedStyle(ancestor);
                const bounds = ancestor.getBoundingClientRect();
                if ((/(hidden|clip)/u.test(css.overflowX) && (box.left < bounds.left || box.right > bounds.right))
                  || (/(hidden|clip)/u.test(css.overflowY) && (box.top < bounds.top || box.bottom > bounds.bottom))) record(ancestor, 'clipping ancestor', box);
              }
            }
          }
        }
      }
      const parentReading = node.parentElement?.closest('[data-region-role="scroll"]');
      const overlay = Boolean(node.closest('[role="dialog"], dialog'));
      regions.push({ id, rect: layoutPosition(node, rect), viewportRect: [rect.x, rect.y, rect.width, rect.height], overflow, overflowDetails, client: [node.clientWidth, node.clientHeight], scroll: [node.scrollWidth, node.scrollHeight], readingChild: Boolean(parentReading), overlay });
      if (overlay) continue;
      for (let parent = node; parent?.parentElement && parent !== document.body; parent = parent.parentElement) {
        if (parent.parentElement.closest('[data-reserved-region], [role="dialog"], dialog')) continue;
        for (const neighbor of parent.parentElement.children) {
          // Dialogs and menus are overlays, so opening or closing one moves no content.
          if (neighbor === parent || neighbor.matches('script, style, link, dialog, [role="dialog"], [role="menu"], [aria-hidden="true"]') || neighbor.querySelector('[data-reserved-region], [role="dialog"], [role="menu"]') || neighbor.matches('[data-reserved-region]') || !visible(neighbor)) continue;
          if (!anchors.has(neighbor)) anchors.set(neighbor, `neighbor-${anchors.size}-${neighbor.tagName.toLowerCase()}`);
        }
      }
    }
    for (const [node, id] of anchors) {
      if (!node.isConnected || !visible(node)) continue;
      const bounds = node.getBoundingClientRect();
      const entry = { id, rect: layoutPosition(node, bounds), overflow: false };
      if (taskTransitions === 'passkey' && node.matches('[data-passkey-task] footer')) {
        const card = regions.find(region => region.id === 'passkey-card')?.rect;
        if (card && node.previousElementSibling) {
          const padding = parseFloat(getComputedStyle(node.previousElementSibling).paddingBlockEnd);
          entry.flowY = Math.max(innerHeight - bounds.height, card[1] + card[3] + padding);
        }
      }
      regions.push(entry);
    }
    const surface = [...document.querySelectorAll('main, #root > *, [data-reserved-region]')].some(node => !node.matches('[data-surface-loading]') && visible(node));
    if (surface || state.startedAt !== null) {
      if (state.startedAt === null) {
        state.startedAt = performance.now();
        for (const id of requiredRegions) if (!ids.has(id)) state.errors.push(`${id}: missing on first surface frame`);
      }
      // The popup's task is the set of sections it shows.
      const task = taskTransitions === 'provisioning'
        ? document.querySelector('.provisioning-stage')?.dataset.complete === 'true' ? 'finalization-complete'
          : document.querySelector('.provisioning-stage .step[aria-current="step"]')?.dataset.step ?? null
        : taskTransitions === 'popup'
          ? [...document.querySelectorAll('.popup-content > [data-region-content] > *')].filter(visible).map(node => node.id || node.dataset.reservedRegion).join(' ')
          : taskTransitions === 'passkey'
            ? document.querySelector('[data-passkey-task]')?.dataset.passkeyTask ?? null
          : taskTransitions === 'dashboard'
            ? (() => {
              const dashboard = document.querySelector('[data-dashboard-task]');
              const sections = dashboard?.querySelectorAll('[data-dashboard-section]');
              return sections ? `dashboard:${JSON.stringify([...sections].map(section => [section.dataset.dashboardSection,
                ...[...section.querySelectorAll('[data-dashboard-task-part]')].map(part => part.dataset.dashboardTaskPart)].join('|')))}` : null;
            })()
          : taskTransitions ? document.querySelector('main[data-presentation-ready="true"]')?.dataset.requirement ?? null : null;
      const signature = JSON.stringify({ task, regions });
      if (signature !== state.signature || state.shifts.length) {
        state.frames.push({ at: performance.now(), nativeAt: nativeNow(), task, regions, shifts: state.shifts.splice(0), errors: [...state.errors] });
        state.signature = signature;
      }
    }
  }
  let stopped = false, frameHandle;
  const scheduleFrame = nativeTimeline ? nativeFrame : callback => requestAnimationFrame(callback);
  const tick = () => {
    if (stopped) return;
    const previousFrame = state.frames.at(-1);
    if (previousFrame && previousFrame.paintThrough === undefined) {
      previousFrame.paintThrough = performance.now();
      previousFrame.nativePaintThrough = nativeNow();
    }
    sample();
    frameHandle = scheduleFrame(tick);
  };
  frameHandle = scheduleFrame(tick);
  const requireNativeClock = () => {
    if (!nativeTimeline || !clockVerified || stopped) throw new Error('Native observation timeline is unavailable or closed');
  };
  const settleNative = async () => {
    requireNativeClock();
    // A task after native rendering lets layout-shift records become available.
    // takeRecords also retains entries not yet delivered to the callback.
    await new Promise(resolve => nativeFrame(() => nativeFrame(() => nativeTask(resolve, 0))));
    recordShifts(shiftObserver.takeRecords());
  };
  window.__regionWatchControl = {
    boundary() {
      requireNativeClock();
      return { at: performance.now(), nativeAt: nativeNow(), frame: state.frames.length, boxes: state.frames.at(-1)?.regions };
    },
    settle: settleNative,
    async finish() {
      await settleNative();
      sample();
      state.nativeTimeline.endAt = nativeNow();
      state.nativeTimeline.completed = true;
      stopped = true;
      nativeCancelFrame(frameHandle);
      shiftObserver.disconnect();
    },
  };
}

export async function installWatcher(page, options = {}) { await page.addInitScript(observeRegions, options); }
export async function nativeBoundary(page) { return page.evaluate(() => window.__regionWatchControl.boundary()); }
export async function settleNativeWatcher(page) { await page.evaluate(() => window.__regionWatchControl.settle()); }

export async function auditScrolling(page) {
  const count = await page.evaluate(() => {
    const root = document.scrollingElement;
    const candidates = [...document.querySelectorAll('*')].filter((node) => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden' && (node === root || /(auto|scroll)/u.test(getComputedStyle(node).overflowY)) && node.scrollHeight > node.clientHeight);
    window.__scrollTargets = [...new Set(candidates.map((node) => node === document.body && getComputedStyle(document.documentElement).overflowY === 'visible' ? root : node))];
    return window.__scrollTargets.length;
  });
  const reports = [];
  for (let index = 0; index < count; index++) {
    const before = await page.evaluate((index) => { const node = window.__scrollTargets[index]; return { top: node.scrollTop, height: node.clientHeight, contentHeight: node.scrollHeight, region: node.closest('[data-reserved-region]')?.dataset.reservedRegion ?? 'page' }; }, index);
    const positions = [];
    for (const fraction of [0, 0.5, 1]) {
      await page.evaluate(({ index, fraction }) => { const node = window.__scrollTargets[index]; node.scrollTop = (node.scrollHeight - node.clientHeight) * fraction; }, { index, fraction });
      await page.evaluate(() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))));
      positions.push(await page.evaluate((index) => { const node = window.__scrollTargets[index]; return { top: node.scrollTop, horizontalEscape: node.scrollWidth > node.clientWidth, endReached: node.scrollTop + node.clientHeight >= node.scrollHeight - 1 }; }, index));
    }
    await page.evaluate(({ index, top }) => { window.__scrollTargets[index].scrollTop = top; }, { index, top: before.top });
    reports.push({ ...before, positions });
  }
  return reports;
}

export async function readWatcher(page) {
  await page.evaluate(() => window.__regionWatcher?.nativeTimeline
    ? window.__regionWatcher.nativeTimeline.completed ? undefined : window.__regionWatchControl.finish()
    : new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const state = await page.evaluate(() => window.__regionWatcher);
  if (!state) throw new Error('Region observer missing');
  const frames = state.frames;
  return { firstPaint: state.firstPaint, firstSurfaceFrame: state.startedAt, frames, rawShifts: state.rawShifts, nativeTimeline: state.nativeTimeline,
    samples: frames.length, regions: [...new Set(frames.flatMap((frame) => frame.regions.map((region) => region.id)))], failures: [...new Set([...state.errors, ...inspectFrames(frames)])] };
}

export async function assertWatcher(page) {
  const report = await readWatcher(page);
  if (!report.samples) throw new Error('No reserved regions were measured');
  if (report.failures.length) throw new Error(report.failures.join('\n'));
  return report;
}
