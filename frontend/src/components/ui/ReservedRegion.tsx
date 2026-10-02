import { keyframes } from '@emotion/react';
import { Alert, Box, Button, Dialog, DialogContent, DialogTitle, Skeleton, Typography, type SxProps, type Theme } from '@mui/material';
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';

import { RevealGroup } from './RevealGroup';
import { componentTokens } from '../../theme';

const sizes = componentTokens.reserved;
const labelEnter = keyframes({ from: { opacity: 0 }, to: { opacity: 1 } });
type Size = { xs: number; sm?: number };
export function ReservedRegion({ id, size, children, regionRole = 'fixed', sx }: {
  id: string; size: Size; children: ReactNode; regionRole?: 'fixed' | 'scroll'; sx?: SxProps<Theme>;
}) {
  return <Box data-reserved-region={id} data-region-role={regionRole} sx={[
    { blockSize: Object.fromEntries(Object.entries(size).map(([key, value]) => [key, `${value / 16}rem`])), minInlineSize: 0, minBlockSize: 0, position: 'relative',
      '& [data-surface-emphasis]': { transform: 'none !important' },
      ...(regionRole === 'scroll' ? { overflowY: 'auto', scrollbarGutter: 'stable', overscrollBehavior: 'contain' } : {}) },
    ...(Array.isArray(sx) ? sx : [sx]),
  ]}>{children}</Box>;
}

function useContentFit(content: unknown) {
  const probe = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  useLayoutEffect(() => {
    const node = probe.current;
    if (!node) return;
    const measure = () => {
      const box = node.getBoundingClientRect();
      if (!box.width || !box.height) return;
      const labels = [...node.querySelectorAll<HTMLElement>('[data-fit-text]')];
      for (const label of labels) label.textContent = label.dataset.fitText ?? '';
      const outside = (rect: DOMRect) => rect.left < box.left || rect.right > box.right || rect.top < box.top || rect.bottom > box.bottom;
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      let exceeded = false;
      while (walker.nextNode()) {
        const range = document.createRange();
        range.selectNodeContents(walker.currentNode);
        exceeded ||= [...range.getClientRects()].some(outside);
      }
      for (const label of labels) label.textContent = '';
      setOverflow(exceeded);
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    document.fonts?.addEventListener('loadingdone', measure);
    return () => { observer?.disconnect(); document.fonts?.removeEventListener('loadingdone', measure); };
  }, [content]);
  return { probe, overflow };
}
const probeStyle = { position: 'absolute', inset: 0, visibility: 'hidden', contain: 'strict', pointerEvents: 'none' } as const;
const detailStyle = { maxHeight: '60vh', overflow: 'auto', overflowWrap: 'anywhere' } as const;

export interface NoticeContent { title: string; body: string; severity: 'info' | 'warning' | 'error'; details?: string }
export function ReservedNotice({ id, notice }: { id: string; notice: NoticeContent | null }) {
  const [open, setOpen] = useState(false);
  const { probe, overflow } = useContentFit(notice);
  const details = Boolean(notice?.details) || overflow;
  const alertStyle = { height: '100%', p: '12px', '& .MuiAlert-message': { p: 0, minWidth: 0 }, '& p': { lineHeight: '1.25rem', overflowWrap: 'anywhere' } };
  const copy = <><Typography variant="subtitle2" component="p">{notice?.title}</Typography><Typography variant="body2">{notice?.body}</Typography></>;
  return <ReservedRegion id={id} size={{ xs: sizes.notice.narrow, sm: sizes.notice.wide }}>
    <Box ref={probe} aria-hidden sx={probeStyle}><Alert role="presentation" severity={notice?.severity ?? 'info'} sx={alertStyle}><Typography variant="subtitle2" component="p" data-fit-text={notice?.title} /><Typography variant="body2" data-fit-text={notice?.body} /></Alert></Box>
    <Alert data-region-content severity={notice?.severity ?? 'info'} sx={{ ...alertStyle, visibility: notice ? 'visible' : 'hidden' }}>
      {details ? <><Typography variant="body2">Details available</Typography><Button size="small" onClick={() => setOpen(true)}>Show details</Button></> : copy}
    </Alert>
    <Dialog open={open && notice !== null} onClose={() => setOpen(false)}><DialogTitle>Details</DialogTitle><DialogContent sx={detailStyle}>
      <Typography variant="subtitle2">{notice?.title}</Typography><Typography>{notice?.body}</Typography>{notice?.details && <Typography>{notice.details}</Typography>}
    </DialogContent><Button onClick={() => setOpen(false)}>Close</Button></Dialog>
  </ReservedRegion>;
}

export function StatusLine({ id, text, tone = 'secondary' }: { id: string; text: string | null; tone?: 'secondary' | 'error' }) {
  const [expandedText, setExpandedText] = useState<string | null>(null);
  const open = text !== null && expandedText === text;
  if (expandedText !== null && expandedText !== text) setExpandedText(null);
  const { probe, overflow: details } = useContentFit(text);
  return <><ReservedRegion id={id} size={{ xs: sizes.statusLine.narrow, sm: sizes.statusLine.wide }}>
    <Box ref={probe} aria-hidden sx={probeStyle}><Typography variant="body2" data-fit-text={text} sx={{ lineHeight: '1.25rem', overflowWrap: 'anywhere' }} /></Box>
    <Box data-region-content sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', visibility: text ? 'visible' : 'hidden' }}>
      <Typography variant="body2" role={tone === 'error' && !details ? 'alert' : 'status'} sx={{ lineHeight: '1.25rem', overflowWrap: 'anywhere', color: tone === 'error' ? 'error.main' : 'text.secondary' }}>{details ? 'Details available' : text || '\u00a0'}</Typography>
      {details && <Button onClick={() => setExpandedText(text)} size="small" sx={{ p: 0, minWidth: 0, lineHeight: '1.25rem', flex: 'none' }}>Show details</Button>}
    </Box>
  </ReservedRegion><Dialog open={open} onClose={() => setExpandedText(null)}><DialogTitle>Details</DialogTitle><DialogContent sx={detailStyle}><Typography role={tone === 'error' ? 'alert' : undefined}>{text}</Typography></DialogContent><Button onClick={() => setExpandedText(null)}>Close</Button></Dialog></>;
}

export function ReservedValue({ id, value }: { id: string; value: number | null | undefined }) {
  const label = value == null ? '—' : value > 9_999_999 ? '10M+' : new Intl.NumberFormat('en-US').format(value);
  return <Box component="span" data-reserved-region={id} sx={{ display: 'inline-block', inlineSize: `${sizes.value.chars}ch`, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
    <Box component="span" data-region-content>{value === undefined ? <Skeleton animation={false} /> : label}</Box>
  </Box>;
}

export function LoadingFrame({ label }: { label: string }) {
  const [visible, setVisible] = useState(false);
  useEffect(() => { const timer = setTimeout(() => setVisible(true), sizes.loadingLabelDelayMs); return () => clearTimeout(timer); }, []);
  return <Box aria-busy="true" data-region-content sx={{ p: 3, display: 'grid', gap: 1.5 }}>
    <Box sx={{ height: 20 }}>{visible && <Typography role="status" variant="body2" sx={{ animation: `${labelEnter} 200ms ease-out both`, '@media (prefers-reduced-motion: reduce)': { animation: 'none' } }}>{label}</Typography>}</Box>
    <Skeleton animation={false} variant="rounded" width="40%" height={12} />
    <Skeleton animation={false} variant="rounded" width="64%" height={12} />
  </Box>;
}

export function ReservedSection({ id, size, loading, label, children }: { id: string; size: Size; loading?: boolean; label: string; children: ReactNode }) {
  return <ReservedRegion id={id} size={size}><RevealGroup fallback={<LoadingFrame label={label} />}>
    {loading ? <LoadingFrame label={label} /> : <Box data-region-content>{children}</Box>}
  </RevealGroup></ReservedRegion>;
}

export function summarize(text: string, maximum: number): string {
  const normalized = text.replace(/\s+/gu, ' ').trim();
  const segments = [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(normalized)].map((part) => part.segment);
  if (segments.length <= maximum) return normalized;
  let cut = segments.slice(0, maximum - 1).join('');
  const boundary = cut.lastIndexOf(' ');
  if (boundary > maximum * 0.6) cut = cut.slice(0, boundary);
  return `${cut}…`;
}

export function BoundedSummary({ text, maximum }: { text: string; maximum: number }) {
  const node = useRef<HTMLSpanElement>(null);
  const summary = summarize(text, maximum);
  const [fitted, setFitted] = useState(summary);
  useLayoutEffect(() => {
    const element = node.current;
    const parent = element?.parentElement;
    if (!element || !parent) return;
    const measure = () => {
      const box = parent.getBoundingClientRect();
      if (!box.width || !box.height) { setFitted(summary); return; }
      const previous = element.textContent;
      const range = document.createRange();
      const fits = (value: string) => {
        element.textContent = value;
        range.selectNodeContents(element);
        return [...range.getClientRects()].every((rect) => rect.left >= box.left && rect.right <= box.right && rect.top >= box.top && rect.bottom <= box.bottom);
      };
      let value = summary;
      if (!fits(summary)) {
        const parts = [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(summary)].map((part) => part.segment);
        let low = 0, high = parts.length;
        while (low < high) {
          const middle = Math.ceil((low + high) / 2);
          if (fits(parts.slice(0, middle).join('').trimEnd() + '…')) low = middle;
          else high = middle - 1;
        }
        value = parts.slice(0, low).join('').trimEnd() + '…';
      }
      element.textContent = previous;
      setFitted(value);
    };
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(parent);
    document.fonts?.addEventListener('loadingdone', measure);
    return () => { observer?.disconnect(); document.fonts?.removeEventListener('loadingdone', measure); };
  }, [summary]);
  return <span ref={node} data-region-content>{fitted}</span>;
}
