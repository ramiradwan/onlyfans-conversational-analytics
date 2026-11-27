import CheckCircleOutlined from '@mui/icons-material/CheckCircleOutlined';
import CloudOffOutlined from '@mui/icons-material/CloudOffOutlined';
import DescriptionOutlined from '@mui/icons-material/DescriptionOutlined';
import ErrorOutlineOutlined from '@mui/icons-material/ErrorOutlineOutlined';
import ExtensionOutlined from '@mui/icons-material/ExtensionOutlined';
import ForumOutlined from '@mui/icons-material/ForumOutlined';
import InfoOutlined from '@mui/icons-material/InfoOutlined';
import InsightsOutlined from '@mui/icons-material/InsightsOutlined';
import KeyOutlined from '@mui/icons-material/KeyOutlined';
import LaptopOutlined from '@mui/icons-material/LaptopOutlined';
import LinkOutlined from '@mui/icons-material/LinkOutlined';
import LockOutlined from '@mui/icons-material/LockOutlined';
import PasswordOutlined from '@mui/icons-material/PasswordOutlined';
import PauseCircleOutlineOutlined from '@mui/icons-material/PauseCircleOutlineOutlined';
import PersonOutlineOutlined from '@mui/icons-material/PersonOutlineOutlined';
import StorageOutlined from '@mui/icons-material/StorageOutlined';
import SyncOutlined from '@mui/icons-material/SyncOutlined';
import SystemUpdateAltOutlined from '@mui/icons-material/SystemUpdateAltOutlined';
import WebAssetOutlined from '@mui/icons-material/WebAssetOutlined';
import Box from '@mui/material/Box';
import SvgIcon from '@mui/material/SvgIcon';
import Typography from '@mui/material/Typography';
import { useId, type ReactNode } from 'react';

import '../../theme/onboarding.css';

/** The pill's dot and the gap after it, as onboarding.css draws them. */
const PILL_DOT = 6;
const PILL_GAP = 6;

const spotIcons = {
  account: PersonOutlineOutlined,
  app: LaptopOutlined,
  browser: WebAssetOutlined,
  code: PasswordOutlined,
  data: StorageOutlined,
  extension: ExtensionOutlined,
  history: ForumOutlined,
  insights: InsightsOutlined,
  legal: DescriptionOutlined,
  link: LinkOutlined,
  lock: LockOutlined,
  offline: CloudOffOutlined,
  passkey: KeyOutlined,
  pause: PauseCircleOutlineOutlined,
  sync: SyncOutlined,
  update: SystemUpdateAltOutlined,
};

export function OnboardingSpot({ icon }: { icon: keyof typeof spotIcons }) {
  const Icon = spotIcons[icon];
  return <span className="onboarding-spot" aria-hidden="true"><Icon focusable="false" /></span>;
}

export function OnboardingPill({
  children, tone = 'neutral', labels,
}: {
  children: ReactNode;
  tone?: 'neutral' | 'progress' | 'error';
  /** Every label the pill can show. It keeps the width of the widest, so a new label never rewraps its row. */
  labels?: readonly string[];
}) {
  if (!labels?.length) return <span className="onboarding-pill" data-tone={tone}>
    <span className="onboarding-tone-dot" aria-hidden="true" />
    <span>{children}</span>
  </span>;
  // The widest label sets the width and the outer columns share the rest, so the dot and the label stay centered,
  // as in a section status. The labels are CSS text, so they stay out of the page text and the accessibility tree.
  return <span className="onboarding-pill" data-tone={tone}
    style={{ display: 'inline-grid', gridTemplateColumns: '1fr auto auto 1fr', columnGap: 0 }}>
    {labels.map((text) => <Box aria-hidden component="span" key={text} sx={{
      gridArea: '1 / 1 / 2 / -1', visibility: 'hidden', paddingInlineStart: `${PILL_DOT + PILL_GAP}px`,
      '&::before': { content: `${JSON.stringify(text)} / ""` },
    }} />)}
    <span className="onboarding-tone-dot" aria-hidden="true" style={{ gridArea: '1 / 2' }} />
    <span style={{ gridArea: '1 / 3', marginInlineStart: PILL_GAP }}>{children}</span>
  </span>;
}

export function OnboardingAction({ children, swap }: { children: ReactNode; swap?: string }) {
  return <div className="onboarding-action">
    <div key={swap} className="onboarding-action-content" data-swap={swap !== undefined ? '' : undefined}>
      {children}
    </div>
  </div>;
}

const statusIcons = { neutral: InfoOutlined, done: CheckCircleOutlined, attention: ErrorOutlineOutlined, error: ErrorOutlineOutlined };

export function OnboardingStatus({
  children, tone = 'neutral', animate = false, live = true,
}: { children: ReactNode; tone?: keyof typeof statusIcons | 'pending'; animate?: boolean; live?: boolean }) {
  const Icon = tone === 'pending' ? SvgIcon : statusIcons[tone];
  return <Typography component="div" variant="body2" className="onboarding-status" data-tone={tone} data-arrive={animate ? '' : undefined}
    role={live ? 'status' : undefined}>
    {children != null && children !== false && children !== '' && <>
      <Icon focusable="false">
        {tone === 'pending' && <circle cx="12" cy="12" r="8" fill="none" stroke="currentColor"
          strokeWidth="2.2" strokeDasharray="38 13" strokeLinecap="round" />}
      </Icon>
      <span>{children}</span>
    </>}
  </Typography>;
}

export interface OnboardingCardProps {
  title: ReactNode;
  heading?: 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6';
  spot?: ReactNode;
  eyebrow?: ReactNode;
  body?: ReactNode;
  children?: ReactNode;
  actions?: ReactNode;
  status?: ReactNode;
  composition?: 'static' | 'trust' | 'guided' | 'decision' | 'first-value' | 'extension';
  arrive?: boolean;
}

export function OnboardingCard({
  title, heading: Heading = 'h2', spot, eyebrow, body, children, actions, status,
  composition = 'guided', arrive = false,
}: OnboardingCardProps) {
  const titleId = useId();
  return <section className="onboarding-surface" data-composition={composition}
    data-arrive={arrive ? '' : undefined} aria-labelledby={titleId}>
    {(spot || eyebrow) && <div className="onboarding-surface-head">{spot}{eyebrow}</div>}
    <Heading className="onboarding-surface-title" id={titleId}>{title}</Heading>
    {body && <div className="onboarding-surface-body">{body}</div>}
    {children}
    {actions}
    {status}
  </section>;
}
