import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useNarrowMasterDetail } from '../src/components/inbox/useNarrowMasterDetail';

describe('useNarrowMasterDetail', () => {
  it('opens detail only on narrow screens and returns to the list', () => {
    const { result, rerender } = renderHook(
      ({ narrow }) => useNarrowMasterDetail(narrow),
      { initialProps: { narrow: true } },
    );

    expect(result.current.detailOpen).toBe(false);
    act(() => result.current.openDetail());
    expect(result.current.detailOpen).toBe(true);
    act(() => result.current.closeDetail());
    expect(result.current.detailOpen).toBe(false);

    rerender({ narrow: false });
    act(() => result.current.openDetail());
    expect(result.current.detailOpen).toBe(false);
  });
});
