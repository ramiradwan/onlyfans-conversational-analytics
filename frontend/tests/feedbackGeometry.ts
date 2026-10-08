import { vi } from 'vitest';

export function mockFeedbackOverflow() {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 100, 20));
  const createRange = document.createRange.bind(document);
  vi.spyOn(document, 'createRange').mockImplementation(() => {
    const range = createRange();
    range.getClientRects = () => [new DOMRect(0, 0, 200, 20)] as unknown as DOMRectList;
    return range;
  });
}
