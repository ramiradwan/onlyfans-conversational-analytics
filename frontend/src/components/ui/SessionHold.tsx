import { Box } from '@mui/material';
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

interface SessionHoldProps {
  /** Changes whenever the part starts over for a new session. */
  scope: string;
  /** The account and role the part belongs to. A change starts over without holding. */
  identity: string;
  /** Whether the part has read its state for the current scope. */
  settled: boolean;
  children: ReactNode;
}

/**
 * Keeps a part at its last settled height while it reloads for a new session of the same account, so the
 * parts below it never move. The hold carries only a height, never the previous session's content.
 */
export function SessionHold({ scope, identity, settled, children }: SessionHoldProps) {
  const box = useRef<HTMLDivElement>(null);
  const settledHeight = useRef<number | null>(null);
  const measuring = useRef(false);
  const shown = useRef({ scope, identity });
  const [held, setHeld] = useState<number | null>(null);
  useLayoutEffect(() => {
    const node = box.current;
    if (!node || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => { if (measuring.current) settledHeight.current = node.getBoundingClientRect().height; });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    if (shown.current.scope === scope) return;
    if (shown.current.identity !== identity) settledHeight.current = null;
    shown.current = { scope, identity };
    setHeld(settledHeight.current);
  }, [identity, scope]);
  useLayoutEffect(() => { if (settled) setHeld(null); }, [settled, scope]);
  // Only settled content at its own height is measured.
  const natural = settled && held === null;
  useLayoutEffect(() => {
    measuring.current = natural;
    if (natural && box.current) settledHeight.current = box.current.getBoundingClientRect().height;
  }, [natural]);
  return <Box ref={box} sx={held === null ? undefined : { minBlockSize: `${held}px` }}>{children}</Box>;
}
