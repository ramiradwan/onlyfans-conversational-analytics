import assert from 'node:assert/strict';

export const backgroundTabLayoutCases = [
  [1440, 16, 'light'], [1440, 16, 'dark'], [480, 16, 'dark'],
  ...[1440, 390, 320].flatMap(width => ['light', 'dark'].map(theme => [width, 32, theme])),
];

export function measureBackgroundTabLayout(page) {
  return page.evaluate(() => {
    const rect = ({ x, y, width, height }) => ({ x: x + scrollX, y: y + scrollY, width, height });
    const regions = {
      header: '.onboarding-header', settings: '#open-options', main: 'main', stage: '.extension-stage',
      card: '#journey-card', icon: '#journey-icon', title: '#journey-title', body: '#journey-body',
      badge: '#journey-badge', metrics: '#preview-metrics', pause: '#pause', primary: '#journey-primary',
      row: '.background-tab-row', note: '#background-tab-note', reopen: '#reopen-background-tab',
      feedback: '[data-reserved-region="surface-feedback"]', footer: '.onboarding-footer',
    };
    const boxes = Object.fromEntries(Object.entries(regions).map(([key, selector]) => {
      const element = document.querySelector(selector);
      return [key, element.getClientRects().length ? rect(element.getBoundingClientRect()) : null];
    }));
    const text = Object.fromEntries(Object.entries({
      title: regions.title, body: regions.body, badge: regions.badge, primary: regions.primary,
      note: '#background-tab-note > span:last-child', reopen: regions.reopen,
      footer: '.onboarding-footer > span, #privacy-link',
    }).map(([key, selector]) => [key, [...document.querySelectorAll(selector)].map(element => {
      const range = document.createRange();
      range.selectNodeContents(element);
      const fragments = [...range.getClientRects()].filter(box => box.width && box.height);
      const bound = element.getBoundingClientRect();
      const contained = fragments.every(fragment => {
        if (fragment.left < bound.left || fragment.right > bound.right
          || fragment.top < bound.top || fragment.bottom > bound.bottom) return false;
        for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
          const { overflowX, overflowY } = getComputedStyle(ancestor);
          const clip = ancestor.getBoundingClientRect();
          if (['clip', 'hidden'].includes(overflowX) && (fragment.left < clip.left || fragment.right > clip.right)) return false;
          if (['clip', 'hidden'].includes(overflowY) && (fragment.top < clip.top || fragment.bottom > clip.bottom)) return false;
        }
        return true;
      });
      return { lines: new Set(fragments.map(({ y }) => y)).size, contained, fragments: fragments.map(rect) };
    })]));
    return { boxes, text, viewportHeight: innerHeight, viewportWidth: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      scrollHeight: document.documentElement.scrollHeight, active: document.activeElement.id };
  });
}

export function assertBackgroundTabLayout(open, closed, width, size) {
  const badgeDelta = closed.boxes.badge.height - open.boxes.badge.height;
  const rowDelta = closed.boxes.row.height - open.boxes.row.height;
  const cardDelta = badgeDelta + rowDelta;
  const narrow = width <= 390 && size === 32;
  for (const key of ['header', 'settings', 'icon', 'title']) {
    assert.deepEqual(closed.boxes[key], open.boxes[key], key);
  }
  for (const key of ['body', 'metrics', 'pause', 'primary', 'row']) {
    const { x, y, width, height } = open.boxes[key];
    assert.deepEqual(closed.boxes[key], { x, y: y + badgeDelta, width, height: height + (key === 'row' ? rowDelta : 0) }, key);
  }
  for (const key of ['main', 'stage', 'card']) {
    const prior = open.boxes[key];
    assert.deepEqual(closed.boxes[key], { ...prior, height: prior.height + cardDelta }, key);
  }
  assert.deepEqual(closed.boxes.feedback, { ...open.boxes.feedback, y: open.boxes.feedback.y + cardDelta }, 'Feedback anchor');
  const { x, width: footerWidth, height } = open.boxes.footer;
  for (const [key, snapshot] of [['note', open], ['reopen', closed]]) {
    const { boxes, text, viewportHeight, viewportWidth, scrollHeight, scrollWidth } = snapshot;
    assert.deepEqual(boxes.footer, {
      x, y: Math.max(viewportHeight - height, boxes.main.y + boxes.main.height), width: footerWidth, height,
    }, 'Legal footer anchor');
    assert.equal(Math.round(boxes.footer.y + boxes.footer.height), scrollHeight, 'Reachable Legal footer');
    assert.equal(snapshot.active, 'journey-primary', 'Primary focus');
    assert(scrollWidth <= viewportWidth, 'Horizontal overflow');
    assert.equal(boxes.row.height, boxes[key].height, 'Rendered row height');
    for (const name of [key, 'title', 'body', 'badge', 'primary', 'footer']) {
      for (const entry of text[name]) assert(entry.lines > 0 && entry.contained, `${name} text is clipped or absent`);
    }
    if (width === 1440 && size === 16) {
      assert.equal(text.title[0].lines, 1, 'Title line budget');
      assert(text.body[0].lines <= 2, 'Body line budget');
      assert.equal(text.badge[0].lines, 1, 'Status line budget');
    }
  }
  if (!narrow) {
    assert.equal(badgeDelta, 0, 'Badge height');
    assert.equal(rowDelta, 0, 'Row height');
  }
}
