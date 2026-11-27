import { Box, ButtonBase, Paper, Stack, Typography, styled } from '@mui/material';
import { visuallyHidden } from '@mui/utils';
import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { ComponentProps } from 'react';
import { Link as RouterLink, useLocation } from 'react-router-dom';

import SettingsView from './SettingsView';
import { CommercialActivationControls } from '../components/CommercialActivationControls';
import { CompanionPairingControls } from '../components/CompanionPairingControls';
import { CreatorVaultControls } from '../components/CreatorVaultControls';
import { NlpDependencyControls } from '../components/NlpDependencyControls';
import { ReservedSection } from '../components/ui/ReservedRegion';
import { SETTINGS_SECTION_INDENT } from '../components/ui/SettingsSection';
import type { CompanionPairingApi } from '../services/companionPairingApi';
import type { CreatorVaultApi } from '../services/creatorVaultApi';
import type { HistorySettingsApi } from '../services/historySettingsApi';
import type { NlpDependenciesApi } from '../services/nlpDependenciesApi';
import type { TextAnalysisClient } from '../services/textAnalysisClient';
import type { TextAnalysisReissueApi } from '../services/textAnalysisReissueApi';
import { bridgeTransportStore } from '../store/transportStore';
import { componentTokens } from '../theme';
import { extensionConnection, extensionLabel } from '../utils/statusCopy';

const SettingsSurface = styled(Paper)(({ theme }) => ({
  backgroundColor: theme.vars.palette.background.paper,
  overflow: 'hidden',
  ...theme.effects.cardBorder(theme),
  // From sm up each section's icon hangs in this indent, so all its content shares the title's edge.
  [theme.breakpoints.up('sm')]: {
    '& > [data-settings-section]': { '--settings-indent': SETTINGS_SECTION_INDENT },
  },
  '& > [data-settings-section] [data-surface-emphasis]': {
    backgroundColor: 'transparent',
    backgroundImage: 'none',
    borderRadius: 0,
    boxShadow: 'none',
    paddingInlineStart: `calc(${theme.spacing(3)} + var(--settings-indent, 0px))`,
  },
  '& > [data-settings-section] [data-visual="settings-loading"]': {
    paddingInlineStart: `calc(${theme.spacing(3)} + var(--settings-indent, 0px))`,
  },
  '& > [data-settings-section] [data-surface-emphasis]::before': {
    display: 'none',
  },
  '& > [data-settings-section] + [data-settings-section]': {
    borderTop: `1px solid ${theme.vars.palette.divider}`,
  },
  '& .MuiStack-root > .MuiButton-root:not(.MuiButton-contained)': {
    alignSelf: 'flex-start',
  },
}));

type SettingsSectionId =
  | 'browser-extension'
  | 'message-history'
  | 'full-analytics'
  | 'text-analysis'
  | 'creator-vault';

interface SettingsSection {
  readonly id: SettingsSectionId;
  readonly label: string;
}

interface SettingsGroup {
  readonly id: string;
  readonly title: string;
  readonly sections: readonly SettingsSection[];
}

const SETTINGS_GROUPS: readonly SettingsGroup[] = [
  {
    id: 'collection',
    title: 'Collection',
    sections: [
      { id: 'browser-extension', label: 'Browser extension' },
      { id: 'message-history', label: 'Message history' },
    ],
  },
  {
    id: 'intelligence',
    title: 'Analytics',
    sections: [
      { id: 'full-analytics', label: 'Full analytics' },
      { id: 'text-analysis', label: 'Text analysis' },
    ],
  },
  {
    id: 'privacy',
    title: 'Data and privacy',
    sections: [
      { id: 'creator-vault', label: 'Stored messages' },
    ],
  },
];

const ALL_SECTIONS: readonly SettingsSection[] = SETTINGS_GROUPS.flatMap((group) => group.sections);
const ALL_SECTION_IDS: readonly SettingsSectionId[] = ALL_SECTIONS.map((section) => section.id);
const sectionAnchorSx = { scrollMarginBlockStart: '1rem' };
// The index marks a section only when it needs the creator.
const ATTENTION_LABELS: ReadonlySet<string> = new Set(['Needs attention', 'Not responding']);
const attentionHint = (label: string | null) => label !== null && ATTENTION_LABELS.has(label) ? label : null;
const ATTENTION_SECTIONS: ReadonlySet<SettingsSectionId> = new Set<SettingsSectionId>([
  'browser-extension', 'message-history', 'full-analytics', 'text-analysis',
]);

export interface SettingsWithVaultViewProps {
  browserApi?: ComponentProps<typeof CompanionPairingControls>['browserApi'];
  onboardingApi?: ComponentProps<typeof CompanionPairingControls>['onboarding'];
  historyApi?: HistorySettingsApi;
  pairingApi?: CompanionPairingApi;
  activationApi?: ComponentProps<typeof CommercialActivationControls>['api'];
  vaultApi?: CreatorVaultApi;
  nlpApi?: NlpDependenciesApi;
  textAnalysisClient?: TextAnalysisClient;
  textAnalysisReissueApi?: TextAnalysisReissueApi;
}

export default function SettingsWithVaultView({
  historyApi,
  pairingApi,
  activationApi,
  vaultApi,
  browserApi,
  onboardingApi,
  nlpApi,
  textAnalysisClient,
  textAnalysisReissueApi,
}: SettingsWithVaultViewProps = {}) {
  const transport = useSyncExternalStore(
    bridgeTransportStore.subscribe,
    bridgeTransportStore.getState,
    bridgeTransportStore.getState,
  );
  const location = useLocation();
  const scrollContainerRef = useRef<HTMLDivElement | null>(null);
  const initialHash = location.hash.replace(/^#/, '') as SettingsSectionId;
  const [activeSection, setActiveSection] = useState<SettingsSectionId>(
    ALL_SECTION_IDS.includes(initialHash) ? initialHash : 'browser-extension',
  );
  const [historySummary, setHistorySummary] = useState<string | null>(null);
  const [analyticsSummary, setAnalyticsSummary] = useState<string | null>(null);
  const [textAnalysisAttention, setTextAnalysisAttention] = useState(false);
  // Text analysis appears only where this installation can run it.
  const [textAnalysisAvailable, setTextAnalysisAvailable] = useState(false);
  const shown = (section: SettingsSection) => section.id !== 'text-analysis' || textAnalysisAvailable;
  const groups = SETTINGS_GROUPS.map((group) => ({ ...group, sections: group.sections.filter(shown) }));
  const sections = ALL_SECTIONS.filter(shown);

  // A link to a section applies once per visit, and Back or Forward to it is a new visit. Text analysis takes it when
  // this installation shows the section.
  const anchorVisit = useRef<{ key: string; applied: boolean } | null>(null);
  useEffect(() => {
    if (anchorVisit.current?.key !== location.key) anchorVisit.current = { key: location.key, applied: false };
    const visit = anchorVisit.current;
    const hashId = location.hash.replace(/^#/, '') as SettingsSectionId;
    if (visit.applied || !ALL_SECTION_IDS.includes(hashId)) return;
    setActiveSection(hashId);
    if (hashId === 'text-analysis' && !textAnalysisAvailable) return;
    const target = scrollContainerRef.current?.querySelector<HTMLElement>(`#${hashId}`);
    if (!target) return;
    visit.applied = true;
    target.scrollIntoView?.({ behavior: 'instant', block: 'start' });
    target.focus({ preventScroll: true });
  }, [location.hash, location.key, textAnalysisAvailable]);

  // Keep the index aligned with manual scrolling.
  useEffect(() => {
    const root = scrollContainerRef.current;
    if (!root) return;
    const elements = ALL_SECTION_IDS
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    if (elements.length === 0) return;

    const updateFromScroll = () => {
      const atBottom = root.scrollTop + root.clientHeight >= root.scrollHeight - 24;
      if (atBottom) {
        setActiveSection(ALL_SECTION_IDS[ALL_SECTION_IDS.length - 1]);
        return;
      }
      const rootTop = root.getBoundingClientRect().top;
      let current: SettingsSectionId = ALL_SECTION_IDS[0];
      for (const el of elements) {
        // A hidden section has no boxes and never becomes current.
        if (el.getClientRects().length > 0 && el.getBoundingClientRect().top - rootTop <= 140) {
          current = el.id as SettingsSectionId;
        }
      }
      setActiveSection(current);
    };

    root.addEventListener('scroll', updateFromScroll, { passive: true });
    return () => root.removeEventListener('scroll', updateFromScroll);
  }, []);

  const agent = transport.connection === 'connected' && transport.agent?.creator_account_id === transport.creatorAccountId
    ? transport.agent : null;
  const extConnection = extensionConnection(agent);
  const browserSurface = agent?.browser;
  const extensionSummary = extConnection !== 'connected'
    ? extensionLabel(extConnection)
    : !browserSurface ? 'Not checked'
      : browserSurface.legal_review_required || browserSurface.site_access !== 'granted'
      ? 'Needs attention'
      : browserSurface.capture === 'paused'
        ? 'Paused'
        : browserSurface.capture === 'off' ? 'Off' : 'Connected';

  const sectionHint = (id: SettingsSectionId): string | null => {
    if (id === 'browser-extension') return attentionHint(extensionSummary);
    // Message history reports only the statuses that need the creator.
    if (id === 'message-history') return historySummary;
    if (id === 'full-analytics') return attentionHint(analyticsSummary);
    if (id === 'text-analysis') return textAnalysisAttention ? 'Needs attention' : null;
    return null;
  };

  return (
    <Box
      ref={scrollContainerRef}
      data-scroll-container
      sx={{ flex: 1, minHeight: 0, overflowY: 'auto', scrollbarGutter: 'stable', pb: 5 }}
    >
      <Stack
        data-visual="settings-frame"
        spacing={2.5}
        sx={{ maxWidth: componentTokens.shell.dashboardMaxWidth, mx: 'auto', width: '100%' }}
      >
        <Box>
          <Typography component="h1" variant="h4">Settings</Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.5 }}>
            Manage collection, analytics, and stored messages.
          </Typography>
        </Box>

        {/* A horizontal jump strip on narrow viewports (< md). It scrolls with the page, so nothing above the
            sections moves while they change. */}
        <Box
          component="nav"
          aria-label="Settings sections"
          sx={{
            display: { xs: 'flex', md: 'none' },
            gap: 0.75,
            overflowX: 'auto',
            pb: 1,
            pt: 0.5,
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          {sections.map((section) => {
            const active = activeSection === section.id;
            return (
              <ButtonBase
                key={section.id}
                component={RouterLink}
                to={{ search: location.search, hash: `#${section.id}` }}
                aria-current={active ? 'location' : undefined}
                sx={(theme) => ({
                  px: 1.5,
                  py: 0.75,
                  borderRadius: `${componentTokens.MuiListItemButton.borderRadius}px`,
                  whiteSpace: 'nowrap',
                  flexShrink: 0,
                  typography: 'body2',
                  // One weight for every pill, so marking the current section never resizes the strip.
                  fontWeight: 500,
                  bgcolor: active ? theme.vars.palette.action.selected : 'transparent',
                  color: active ? theme.vars.palette.action.selectedForeground : theme.vars.palette.text.secondary,
                  '&:hover': { bgcolor: theme.vars.palette.action.hover },
                  '&:focus-visible': { outline: `2px solid ${theme.vars.palette.primary.main}`, outlineOffset: 2 },
                })}
              >
                {section.label}
              </ButtonBase>
            );
          })}
        </Box>

        {/* Two columns on desktop, an index beside the settings. One column below md. */}
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: { xs: 'minmax(0, 1fr)', md: 'min(15.5rem, 30%) minmax(0, 1fr)' },
            gap: { xs: 3, md: 4 },
            alignItems: 'start',
          }}
        >
          <Box
            component="nav"
            aria-label="Settings sections"
            sx={{ display: { xs: 'none', md: 'block' } }}
          >
            <Stack spacing={2.5}>
              {groups.map((group) => (
                <Box key={group.id}>
                  <Typography
                    component="p"
                    variant="caption"
                    sx={{
                      color: 'text.muted',
                      fontWeight: 600,
                      display: 'block',
                      px: 1.25,
                      mb: 0.75,
                    }}
                  >
                    {group.title}
                  </Typography>
                  <Stack spacing={0.25}>
                    {group.sections.map((section) => {
                      const active = activeSection === section.id;
                      const hint = sectionHint(section.id);
                      return (
                        <ButtonBase
                          key={section.id}
                          component={RouterLink}
                          to={{ search: location.search, hash: `#${section.id}` }}
                          aria-current={active ? 'location' : undefined}
                          sx={(theme) => ({
                            width: '100%',
                            minWidth: 0,
                            overflow: 'hidden',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            gap: 1.25,
                            px: 1.25,
                            py: 1,
                            borderRadius: `${componentTokens.MuiListItemButton.borderRadius}px`,
                            textAlign: 'left',
                            bgcolor: active ? theme.vars.palette.action.selected : 'transparent',
                            color: active
                              ? theme.vars.palette.action.selectedForeground
                              : theme.vars.palette.text.secondary,
                            '&:hover': {
                              bgcolor: active
                                ? theme.vars.palette.action.selected
                                : theme.vars.palette.action.hover,
                              color: active
                                ? theme.vars.palette.action.selectedForeground
                                : theme.vars.palette.text.primary,
                            },
                            '&:focus-visible': {
                              outline: `2px solid ${theme.vars.palette.primary.main}`,
                              outlineOffset: 2,
                            },
                          })}
                        >
                          <Typography
                            component="span"
                            variant="body2"
                            sx={{
                              color: 'inherit',
                              fontWeight: active ? 600 : 500,
                              minWidth: 0,
                              overflowWrap: 'anywhere',
                            }}
                          >
                            {section.label}
                          </Typography>
                          {/* A section that can need attention keeps a place for its dot, so the dot comes and goes
                              without rewrapping the label. Its status reads with the link. */}
                          {ATTENTION_SECTIONS.has(section.id) && (
                            <Box
                              component="span"
                              aria-hidden
                              sx={(theme) => ({
                                flexShrink: 0,
                                inlineSize: componentTokens.StatusChip.dotSize,
                                blockSize: componentTokens.StatusChip.dotSize,
                                borderRadius: '50%',
                                bgcolor: theme.vars.palette.warning.main,
                                visibility: hint ? 'visible' : 'hidden',
                              })}
                            />
                          )}
                          {hint && <span style={visuallyHidden}>, {hint}</span>}
                        </ButtonBase>
                      );
                    })}
                  </Stack>
                </Box>
              ))}
            </Stack>
          </Box>

          <Stack data-visual="settings-content" spacing={3.5} sx={{ minWidth: 0, maxWidth: 840, width: '100%' }}>
            <Stack spacing={1.25}>
              <Typography component="h2" variant="subtitle2" sx={{ color: 'text.secondary', px: 0.5 }}>
                Collection
              </Typography>
              <SettingsSurface elevation={0}>
                <Box id="browser-extension" data-settings-section tabIndex={-1} sx={sectionAnchorSx}>
                  <ReservedSection grow id="settings-browser" size={{ xs: 0, sm: 0 }} title="Browser extension" label="Loading…">
                    <CompanionPairingControls api={pairingApi} browserApi={browserApi} onboarding={onboardingApi} />
                  </ReservedSection>
                </Box>
                <Box id="message-history" data-settings-section tabIndex={-1} sx={sectionAnchorSx}>
                  <ReservedSection grow id="settings-history" size={{ xs: 0, sm: 0 }} title="Message history" label="Loading…">
                    <SettingsView
                      api={historyApi}
                      browserApi={browserApi}
                      desktopApi={onboardingApi}
                      onStatusLabelChange={setHistorySummary}
                    />
                  </ReservedSection>
                </Box>
              </SettingsSurface>
            </Stack>

            <Stack spacing={1.25}>
              <Typography component="h2" variant="subtitle2" sx={{ color: 'text.secondary', px: 0.5 }}>
                Analytics
              </Typography>
              <SettingsSurface elevation={0}>
                <Box id="full-analytics" data-settings-section tabIndex={-1} sx={sectionAnchorSx}>
                  <ReservedSection grow id="settings-activation" size={{ xs: 0, sm: 0 }} title="Full analytics" label="Loading…">
                    <CommercialActivationControls
                      api={activationApi}
                      onboarding={onboardingApi}
                      creatorAccountId={transport.creatorAccountId}
                      onStatusChange={setAnalyticsSummary}
                    />
                  </ReservedSection>
                </Box>
                {/* Mounted while hidden, so its status loads with the page and showing it moves nothing. */}
                <Box id="text-analysis" data-settings-section tabIndex={-1}
                  sx={{ ...sectionAnchorSx, ...(textAnalysisAvailable ? {} : { display: 'none' }) }}>
                  <ReservedSection grow id="settings-text-analysis" size={{ xs: 0, sm: 0 }} title="Text analysis" label="Loading…">
                    <NlpDependencyControls
                      api={nlpApi}
                      client={textAnalysisClient}
                      reissueApi={textAnalysisReissueApi}
                      creatorAccountId={transport.creatorAccountId}
                      onAttentionChange={setTextAnalysisAttention}
                      onAvailabilityChange={setTextAnalysisAvailable}
                    />
                  </ReservedSection>
                </Box>
              </SettingsSurface>
            </Stack>

            <Stack spacing={1.25}>
              <Typography component="h2" variant="subtitle2" sx={{ color: 'text.secondary', px: 0.5 }}>
                Data and privacy
              </Typography>
              <SettingsSurface elevation={0}>
                <Box id="creator-vault" data-settings-section tabIndex={-1} sx={sectionAnchorSx}>
                  <ReservedSection grow id="settings-vault" size={{ xs: 0, sm: 0 }} title="Stored messages" label="Loading…">
                    <CreatorVaultControls api={vaultApi} />
                  </ReservedSection>
                </Box>
              </SettingsSurface>
            </Stack>
          </Stack>
        </Box>
      </Stack>
    </Box>
  );
}
