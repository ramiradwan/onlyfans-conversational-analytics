import {
  createContext, useContext, useId, useLayoutEffect, useRef, useState, type ReactNode,
} from 'react';

interface RevealRegistry {
  hold: (id: string) => void;
  release: (id: string) => void;
}

const RevealContext = createContext<RevealRegistry | null>(null);

/**
 * Shows its children in one frame once every section holding the group has settled, so sections
 * that load at different speeds never move each other. `fallback` renders until then.
 */
export function RevealGroup({ children, fallback }: {
  children: ReactNode;
  fallback: ReactNode;
}) {
  const [revealed, setRevealed] = useState(false);
  const pending = useRef(new Set<string>());
  const mounted = useRef(false);
  const [registry] = useState<RevealRegistry>(() => ({
    hold: (id) => pending.current.add(id),
    release: (id) => {
      pending.current.delete(id);
      if (mounted.current && pending.current.size === 0) setRevealed(true);
    },
  }));

  // Runs after the sections' own layout effects, so every initial hold is already registered.
  useLayoutEffect(() => {
    mounted.current = true;
    if (pending.current.size === 0) setRevealed(true);
    return () => { mounted.current = false; };
  }, []);


  return (
    <RevealContext.Provider value={registry}>
      {!revealed && fallback}
      <div hidden={!revealed}>{children}</div>
    </RevealContext.Provider>
  );
}

/** Holds the enclosing `RevealGroup` while `pending` is true. Does nothing outside a group. */
export function useRevealHold(pending: boolean): void {
  const registry = useContext(RevealContext);
  const id = useId();
  useLayoutEffect(() => {
    if (!pending || registry === null) return;
    registry.hold(id);
    return () => registry.release(id);
  }, [id, pending, registry]);
}
