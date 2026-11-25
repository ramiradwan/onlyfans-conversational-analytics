import { useEffect, useRef } from 'react';

import { followPasskeyOpen } from '../../shared-setup/passkey-window.js';

export function usePasskeyOpen(ready: boolean, unlock: () => void) {
  const current = useRef({ ready, unlock });
  current.current = { ready, unlock };
  const listener = useRef<ReturnType<typeof followPasskeyOpen> | null>(null);

  useEffect(() => {
    const open = followPasskeyOpen(() => current.current.ready, () => current.current.unlock());
    listener.current = open;
    return () => { open.stop(); listener.current = null; };
  }, []);
  useEffect(() => { if (ready) listener.current?.check(); }, [ready]);
}
