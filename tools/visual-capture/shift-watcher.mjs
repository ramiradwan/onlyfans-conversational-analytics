export function inspectFrames(frames) {
  const baseline = new Map();
  const issues = new Set();
  for (const frame of frames) {
    const present = new Set();
    for (const region of frame.regions) {
      present.add(region.id);
      if (region.overflow) issues.add(`${region.id}: overflow`);
      const before = baseline.get(region.id);
      if (before && !region.readingChild && region.rect.some((value, index) => value !== before[index])) issues.add(`${region.id}: moved or resized`);
      if (!before && !region.readingChild) baseline.set(region.id, region.rect);
    }
    for (const id of baseline.keys()) if (!present.has(id)) issues.add(`${id}: removed`);
    if (frame.shifts?.some((entry) => entry.value > 0)) issues.add('layout shift after first paint');
  }
  return [...issues];
}

export function observeRegions() {
  const state = { frames: [], startedAt: null, shifts: [], signature: '' };
  window.__regionWatcher = state;
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) if (state.startedAt !== null && entry.startTime >= state.startedAt) state.shifts.push({ value: entry.value, hadRecentInput: entry.hadRecentInput,
      sources: (entry.sources ?? []).map(({ node, previousRect, currentRect }) => ({ tag: node?.nodeName, region: node?.closest?.('[data-reserved-region]')?.dataset.reservedRegion ?? null,
        from: [previousRect.x, previousRect.y, previousRect.width, previousRect.height], to: [currentRect.x, currentRect.y, currentRect.width, currentRect.height] })) });
  }).observe({ type: 'layout-shift', buffered: true });
  const visible = (node) => {
    const css = getComputedStyle(node);
    const screenReaderOnly = css.position === 'absolute' && css.width === '1px' && css.height === '1px' && (css.clip !== 'auto' || css.clipPath !== 'none');
    return node.getClientRects().length && css.visibility !== 'hidden' && !screenReaderOnly;
  };
  const beyond = (rect, outer) => rect.left < outer.left - 0.01 || rect.right > outer.right + 0.01 || rect.top < outer.top - 0.01 || rect.bottom > outer.bottom + 0.01;
  function layoutPosition(node, rect) {
    let x = rect.x, y = rect.y, fixed = false;
    for (let parent = node; parent; parent = parent.parentElement) {
      const css = getComputedStyle(parent);
      fixed ||= css.position === 'fixed';
      if (css.transform !== 'none') { const matrix = new DOMMatrix(css.transform); x -= matrix.m41; y -= matrix.m42; }
      if (parent !== node && parent !== document.body && parent !== document.documentElement) { x += parent.scrollLeft; y += parent.scrollTop; }
    }
    return [x + (fixed ? 0 : scrollX), y + (fixed ? 0 : scrollY), rect.width, rect.height];
  }
  function sample() {
    const regions = [];
    const ids = new Set();
    for (const node of document.querySelectorAll('[data-reserved-region]')) {
      if (!node.getClientRects().length) continue;
      const id = node.dataset.reservedRegion;
      const rect = node.getBoundingClientRect();
      const reading = node.dataset.regionRole === 'scroll';
      let overflow = ids.has(id);
      ids.add(id);
      if (visible(node)) {
        const content = [...node.querySelectorAll('[data-region-content]')].filter((child) => child.closest('[data-reserved-region]') === node);
        for (const child of content) {
          if (!visible(child)) continue;
          if (reading) {
            if (!/(auto|scroll)/u.test(getComputedStyle(node).overflowY) && !node.querySelector('[data-reading-viewport]')) overflow = true;
            continue;
          }
          if (beyond(child.getBoundingClientRect(), rect)) overflow = true;
          const walker = document.createTreeWalker(child, NodeFilter.SHOW_TEXT);
          while (walker.nextNode()) {
            const text = walker.currentNode;
            if (!text.textContent.trim() || !visible(text.parentElement)) continue;
            const readingViewport = text.parentElement.closest('[data-region-role="scroll"]');
            if (readingViewport && readingViewport !== node && node.contains(readingViewport)) continue;
            const range = document.createRange(); range.selectNodeContents(text);
            for (const box of range.getClientRects()) if (beyond(box, rect)) overflow = true;
          }
        }
      }
      const parentReading = node.parentElement?.closest('[data-region-role="scroll"]');
      regions.push({ id, rect: layoutPosition(node, rect), overflow, readingChild: Boolean(parentReading) });
    }
    if (regions.length) {
      if (state.startedAt === null) state.startedAt = performance.now();
      const signature = JSON.stringify(regions);
      if (signature !== state.signature || state.shifts.length) {
        state.frames.push({ regions, shifts: state.shifts.splice(0) });
        state.signature = signature;
      }
    }
    requestAnimationFrame(sample);
  }
  requestAnimationFrame(sample);
}

export async function installWatcher(page) { await page.addInitScript(observeRegions); }

export async function readWatcher(page) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const frames = await page.evaluate(() => window.__regionWatcher?.frames ?? []);
  return { samples: frames.length, regions: [...new Set(frames.flatMap((frame) => frame.regions.map((region) => region.id)))], failures: inspectFrames(frames) };
}

export async function assertWatcher(page) {
  const report = await readWatcher(page);
  if (!report.samples) throw new Error('No reserved regions were measured');
  if (report.failures.length) throw new Error(report.failures.join('\n'));
  return report;
}
