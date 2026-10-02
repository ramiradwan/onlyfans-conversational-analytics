import { keyframes } from '@emotion/react';
import { Box, Button, ButtonBase, Popover, Stack, Typography } from '@mui/material';
import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react';

import type { CatchupFreshness } from '../../protocol';
import { defaultExtensionPort } from '../../services/extensionPort';
import type { BridgeConnectionState } from '../../store/transportStore';
import { componentTokens } from '../../theme';
import { connectionGrace, connectionNote } from '../../utils/connectionGrace';
import { freshnessRows, presentFreshness, viewerTimeZone, type FreshnessPresentation } from '../../utils/freshnessPresentation';

const tokens = componentTokens.FreshnessStatus;
const breathe = keyframes({ from: { opacity: 0.45 }, to: { opacity: 1 } });
const fade = keyframes({ from: { opacity: 0 }, to: { opacity: 1 } });

export function FreshnessStatus({ freshness, bridge, snapshotUsable, now = new Date(), timeZone = viewerTimeZone(), override }: {
  freshness: CatchupFreshness | null | undefined; bridge: BridgeConnectionState; snapshotUsable: boolean;
  now?: Date; timeZone?: string; override?: FreshnessPresentation;
}) {
  const current = override ?? presentFreshness({ freshness, bridge, snapshotUsable, now, timeZone });
  const [held, setHeld] = useState<{ value: FreshnessPresentation; since: number }>(() => ({ value: current, since: performance.now() }));
  const latest = useRef(current);
  latest.current = current;
  const key = `${current.label}:${current.icon}:${current.sentence}`;
  const hold = held.value.icon === 'ring-active' && current.tone === 'settled' && current.icon === 'dot'
    && performance.now() - held.since < tokens.minCheckingMs;
  const shown = hold ? held.value : current;
  useEffect(() => {
    if (`${held.value.label}:${held.value.icon}:${held.value.sentence}` === key) return;
    if (hold) {
      const timer = setTimeout(() => setHeld({ value: latest.current, since: performance.now() }), Math.max(0, tokens.minCheckingMs - (performance.now() - held.since)));
      return () => clearTimeout(timer);
    }
    setHeld({ value: latest.current, since: performance.now() });
  }, [key, hold, held]);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const id = useId();
  const phase = useSyncExternalStore(connectionGrace.subscribe, connectionGrace.getSnapshot);
  const note = connectionNote(phase);
  const [navigation, setNavigation] = useState<'creator' | 'setup' | null>(null);
  useEffect(() => {
    if (navigation === null) return;
    const port = defaultExtensionPort();
    let finished = false;
    const openWhenConnected = () => {
      const status = port.getState().status;
      if (finished || status === 'connecting') return;
      finished = true;
      if (status === 'connected') port.open(navigation);
      setNavigation(null);
    };
    const unsubscribe = port.subscribe(openWhenConnected);
    openWhenConnected();
    return unsubscribe;
  }, [navigation]);
  const rows = snapshotUsable ? freshnessRows(freshness, now, timeZone) : [];
  const color = shown.icon === 'ring-active' ? 'brand.main' : ({ settled: 'success.main', checking: 'brand.main', unknown: 'text.disabled', attention: 'warning.main', user: 'text.secondary', error: 'error.main' })[shown.tone];
  return <>
    <ButtonBase data-reserved-region="freshness-status" data-region-role="fixed" aria-haspopup="dialog" aria-expanded={Boolean(anchor)} aria-controls={anchor ? id : undefined}
      aria-label={`Status: ${shown.label}. Show details`} onClick={(event) => setAnchor(event.currentTarget)}
      sx={{ inlineSize: tokens.inlineSize, blockSize: tokens.blockSize, flex: 'none', fontSize: tokens.fontSize, lineHeight: `${tokens.blockSize}px`, px: `${tokens.paddingInline}px`,
        display: 'grid', gridTemplateColumns: `${tokens.iconGutter}px minmax(0,1fr)`, columnGap: `${tokens.gap}px`, whiteSpace: 'nowrap', textAlign: 'start', fontVariantNumeric: 'tabular-nums',
        borderRadius: '999px', color: shown.tone === 'settled' ? 'text.secondary' : 'text.primary', transition: 'background-color 120ms', '&:hover': { bgcolor: 'action.hover' }, '&:focus-visible': { outline: '2px solid', outlineColor: 'brand.main' } }}>
      <Box key={shown.icon} aria-hidden sx={{ display: 'grid', placeItems: 'center', width: tokens.iconGutter, animation: `${fade} 200ms ease-out both`, '@media (prefers-reduced-motion: reduce)': { animation: 'none' } }}><Box data-freshness-glyph={shown.icon} sx={{ width: tokens.dotSize, height: tokens.dotSize, color,
        ...(shown.icon === 'pause' ? { borderLeft: '2.5px solid', borderRight: '2.5px solid' } : { borderRadius: '50%', ...(shown.icon === 'dot' ? { bgcolor: color } : { border: '1.5px solid' }) }),
        ...(shown.icon === 'ring-active' ? { animation: `${breathe} 320ms ease-in-out infinite alternate` } : {}), '@media (prefers-reduced-motion: reduce)': { animation: 'none' } }} /></Box>
      <Box component="span" data-region-content key={`${shown.label}:${shown.icon}`} sx={{ animation: `${fade} 200ms ease-out both`, '@media (prefers-reduced-motion: reduce)': { animation: 'none' } }}>{shown.label}</Box>
    </ButtonBase>
    <Box aria-live="polite" sx={{ position: 'absolute', width: 1, height: 1, overflow: 'hidden', clipPath: 'inset(50%)' }}>{shown.label}</Box>
    <Popover id={id} open={Boolean(anchor)} anchorEl={anchor} onClose={() => setAnchor(null)} disableScrollLock anchorOrigin={{ horizontal: 'right', vertical: 'bottom' }} transformOrigin={{ horizontal: 'right', vertical: 'top' }}
      slotProps={{ paper: { role: 'dialog', 'aria-label': 'Status details', sx: { width: tokens.detailInlineSize, maxWidth: 'calc(100vw - 32px)' } } }}>
      <Stack spacing={1.5} sx={{ p: 2 }}>
        <Box><Typography variant="subtitle2">{current.title}</Typography><Typography variant="body2">{current.sentence}</Typography></Box>
        <Box component="dl" sx={{ display: 'grid', gap: 1, m: 0 }}>{[...(note ? [{ label: 'Connection', value: note }] : []), ...rows].slice(0, 3).map((row) => <Box key={row.label}><Typography component="dt" variant="caption">{row.label}</Typography><Typography component="dd" variant="body2" sx={{ m: 0, fontVariantNumeric: 'tabular-nums' }}>{row.value}</Typography></Box>)}</Box>
        {current.action && <Button size="small" variant="outlined" component={current.action.destination === 'settings' ? 'a' : 'button'} href={current.action.destination === 'settings' ? '/settings#browser-extension' : undefined}
          onClick={() => { const destination = current.action?.destination; if (destination === 'reload') window.location.reload(); else if (destination === 'creator' || destination === 'setup') setNavigation(destination); setAnchor(null); }} sx={{ alignSelf: 'flex-start' }}>{current.action.label}</Button>}
      </Stack>
    </Popover>
  </>;
}
