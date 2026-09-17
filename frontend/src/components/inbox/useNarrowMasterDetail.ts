import { useCallback, useEffect, useState } from 'react';

/** Keeps narrow inbox navigation separate from the canonical conversation selection. */
export function useNarrowMasterDetail(isNarrow: boolean) {
  const [detailOpen, setDetailOpen] = useState(false);

  useEffect(() => {
    if (!isNarrow) setDetailOpen(false);
  }, [isNarrow]);

  const openDetail = useCallback(() => {
    if (isNarrow) setDetailOpen(true);
  }, [isNarrow]);
  const closeDetail = useCallback(() => setDetailOpen(false), []);

  return {
    detailOpen: isNarrow && detailOpen,
    openDetail,
    closeDetail,
  } as const;
}
