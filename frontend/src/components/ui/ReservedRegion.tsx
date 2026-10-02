import { keyframes } from '@emotion/react';
import { Alert, Box, Button, Dialog, DialogContent, DialogTitle, Skeleton, Typography, type SxProps, type Theme } from '@mui/material';
import { useEffect, useState, type ReactNode } from 'react';

import { RevealGroup } from './RevealGroup';
import { componentTokens } from '../../theme';

const sizes = componentTokens.reserved;
const labelEnter = keyframes({ from: { opacity: 0 }, to: { opacity: 1 } });
type Size = { xs: number; sm?: number };
export function ReservedRegion({ id, size, children, regionRole = 'fixed', sx }: {
  id: string; size: Size; children: ReactNode; regionRole?: 'fixed' | 'scroll'; sx?: SxProps<Theme>;
}) {
  return <Box data-reserved-region={id} data-region-role={regionRole} sx={[
    { blockSize: size, minInlineSize: 0, minBlockSize: 0, position: 'relative',
      ...(regionRole === 'scroll' ? { overflowY: 'auto', scrollbarGutter: 'stable', overscrollBehavior: 'contain' } : {}) },
    ...(Array.isArray(sx) ? sx : [sx]),
  ]}>{children}</Box>;
}

export interface NoticeContent { title: string; body: string; severity: 'info' | 'warning' | 'error'; details?: string }
export function ReservedNotice({ id, notice }: { id: string; notice: NoticeContent | null }) {
  const [open, setOpen] = useState(false);
  return <ReservedRegion id={id} size={{ xs: sizes.notice.narrow, sm: sizes.notice.wide }}>
    <Alert data-region-content severity={notice?.severity ?? 'info'} sx={{ height: '100%', visibility: notice ? 'visible' : 'hidden', p: '12px', '& .MuiAlert-message': { p: 0 }, '& p': { lineHeight: '20px' } }}>
      <Typography variant="subtitle2" component="p">{notice?.title}</Typography>
      <Typography variant="body2">{notice?.details ? 'Details available' : notice?.body}</Typography>
      {notice?.details && <Button size="small" onClick={() => setOpen(true)}>Show details</Button>}
    </Alert>
    <Dialog open={open} onClose={() => setOpen(false)}><DialogTitle>Details</DialogTitle><DialogContent sx={{ maxHeight: '60vh', overflow: 'auto' }}>{notice?.details}</DialogContent><Button onClick={() => setOpen(false)}>Close</Button></Dialog>
  </ReservedRegion>;
}

export function StatusLine({ id, text, tone = 'secondary' }: { id: string; text: string | null; tone?: 'secondary' | 'error' }) {
  const [expandedText, setExpandedText] = useState<string | null>(null);
  const open = text !== null && expandedText === text;
  if (expandedText !== null && expandedText !== text) setExpandedText(null);
  const details = Boolean(text && text.length > 60);
  return <><ReservedRegion id={id} size={{ xs: sizes.statusLine.narrow, sm: sizes.statusLine.wide }}>
    <Box data-region-content sx={{ display: 'flex', gap: 1, alignItems: 'baseline', visibility: text ? 'visible' : 'hidden' }}>
      <Typography variant="body2" role={tone === 'error' && !details ? 'alert' : 'status'} sx={{ lineHeight: '20px', color: tone === 'error' ? 'error.main' : 'text.secondary' }}>{details ? 'Details available' : text || '\u00a0'}</Typography>
      {details && <Button onClick={() => setExpandedText(text)} size="small" sx={{ p: 0, minWidth: 0, lineHeight: '20px' }}>Show details</Button>}
    </Box>
  </ReservedRegion><Dialog open={open} onClose={() => setExpandedText(null)}><DialogTitle>Details</DialogTitle><DialogContent sx={{ maxHeight: '60vh', overflow: 'auto' }}><Typography role={tone === 'error' ? 'alert' : undefined}>{text}</Typography></DialogContent><Button onClick={() => setExpandedText(null)}>Close</Button></Dialog></>;
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
