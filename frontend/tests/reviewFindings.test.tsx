import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { formatRatioPercentParts, formatMinutesParts, formatDecimal, formatRatioPercent } from '../src/analytics';
import { ResponseOverview } from '../src/components/analytics/ResponseOverview';
import { StatusChip } from '../src/components/ui/StatusChip';
import { theme } from '../src/theme';
import { componentTokens, semanticIntents, effectTokens, typography } from '../src/theme/generated/tokens';
import { contrastRatio, contrastRatioOnComposite } from '../src/theme/color-validation';
import { generateThemeSource } from '../src/theme/generate-theme';
import { storyAnalyticsModel } from '../src/story-only/analyticsFixtures';

afterEach(cleanup);
const fresh = () => JSON.parse(fs.readFileSync('src/theme/tokens.json', 'utf8'));

describe('reviewed chrome contracts', () => {
  it.each(['light', 'dark'] as const)('orders %s selection, hover and rest by actual contrast', (mode) => {
    const { action, surface, text } = semanticIntents[mode];
    expect(action.state.focus).toBe(action.state.hover);
    expect(contrastRatio(action.state.selected, surface.paper)).toBeGreaterThan(contrastRatio(action.state.hover, surface.paper));
    expect(contrastRatio(action.state.selectedForeground, surface.paper)).toBeGreaterThan(contrastRatio(text.secondary, surface.paper));
    expect(contrastRatio(text.secondary, surface.paper)).toBeGreaterThan(contrastRatio(text.muted, surface.paper));
    expect(contrastRatioOnComposite(surface.tooltip.text, surface.tooltip.fill, surface.canvas)).toBeGreaterThanOrEqual(4.5);
  });
  it('uses the authored icon-button focus layer without the default pulsating ripple', () => {
    expect(theme.components?.MuiIconButton?.defaultProps?.disableFocusRipple).toBe(true);
    expect(theme.components?.MuiIconButton?.defaultProps?.disableRipple).not.toBe(true);
  });
  it('derives the concentric rail dimensions from the existing radii', () => {
    const inset = componentTokens.MuiPaper.borderRadius - componentTokens.MuiListItemButton.borderRadius;
    expect(inset).toBe(8); expect(componentTokens.shell.desktopRailWidth - 2 * inset).toBe(48);
  });
  it('rejects violet neutral chrome and weak tooltip text during generation', () => {
    const violet = fresh(); violet.tier2.intents.light.action.state.disabled = 'oklch(19.2637% 0.03002 289.38 / 0.30)';
    expect(() => generateThemeSource(JSON.stringify(violet))).toThrow(/violet leaked/);
    const weak = fresh(); weak.tier2.intents.light.surface.tooltip.text = 'oklch(21.9569% 0.00360 164.71)';
    expect(() => generateThemeSource(JSON.stringify(weak))).toThrow(/tooltip text contrast/);
  });
  it('keeps the original app mark and all popup disclosures unchanged', () => {
    const baseline = JSON.parse(fs.readFileSync('tests/fixtures/review-content-baseline.json', 'utf8'));
    for (const [file, hash] of Object.entries(baseline.sha256)) {
      expect(createHash('sha256').update(fs.readFileSync('../' + file, 'utf8').replace(/\r\n/g, '\n')).digest('hex'), file).toBe(hash);
    }
  });
});

describe('metric text and missing values', () => {
  it.each(['en-US', 'de-DE', 'fr-FR', 'tr-TR', 'ar-EG'])('preserves %s percent placement and spacing', (locale) => {
    for (const value of [0, 0.75, 1]) {
      expect(formatRatioPercentParts(value, 0, locale).map((part) => part.value).join(''))
        .toBe(new Intl.NumberFormat(locale, { style: 'percent', minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(value));
    }
  });
  it('keeps the existing minute wording and decimal precision', () => {
    expect(formatMinutesParts(6.4).map((part) => part.value).join('')).toBe(formatDecimal(6.4) + ' min');
  });
  it.each([null, 0, 0.75, 1])('draws only the known coverage ratio %s', (coverage) => {
    const metrics = { ...storyAnalyticsModel.response, responseCoverage: coverage, averageHandlingMinutes: coverage === null ? null : 6.4 };
    const { container } = render(<ThemeProvider theme={theme}><ResponseOverview metrics={metrics} /></ThemeProvider>);
    const values = [...container.querySelectorAll('dd')].map((node) => node.textContent);
    expect(values[0]).toBe(coverage === null ? '—' : formatDecimal(6.4) + ' min');
    expect(values[1]).toBe(coverage === null ? '—' : formatRatioPercent(coverage));
    const fill = container.querySelector<HTMLElement>('[data-visual="reply-coverage-fill"]');
    if (coverage === null) expect(fill).toBeNull();
    else expect(fill?.style.width).toBe(`${coverage * 100}%`);
  });
});

describe('review typography and motion', () => {
  it('reserves the 72px KPI for the dashboard and names the smaller visual roles', () => {
    expect(typography.insight.fontSize).toBe('2.875rem');
    expect(typography.metric.letterSpacing).toBe('-0.03em');
    expect(typography.metricUnit.fontSize).toBe('1.25rem');
    expect(typography.passkeyTitle.fontSize).toBe('1.625rem');
    expect(componentTokens.analytics.barThickness).toBe(7);
  });
  it('authors only transform and opacity keyframes under the existing timing budget', () => {
    expect(effectTokens.motion.duration).toEqual({ fast: '120ms', standard: '200ms', spatial: '320ms' });
    for (const animation of Object.values(effectTokens.motion.keyframes)) {
      for (const frame of Object.values(animation)) expect(Object.keys(frame).every((key) => ['opacity', 'transform'].includes(key))).toBe(true);
    }
    expect(fs.readFileSync('src/theme/presentationMotion.ts', 'utf8')).toContain("animation: 'none'");
    expect(fs.readFileSync('src/theme/presentationMotion.ts', 'utf8')).not.toContain('infinite');
  });
  it('settles only explicitly healthy statuses, never warnings or every SectionHeader', () => {
    const view = (tone: 'success' | 'error', settled = true) => <ThemeProvider theme={theme}>
      <StatusChip label="Status" tone={tone} settled={settled} />
    </ThemeProvider>;
    const { container, rerender } = render(view('success'));
    expect(container.querySelector('[data-status-settled="true"]')).not.toBeNull();
    rerender(view('error')); expect(container.querySelector('[data-status-settled="true"]')).toBeNull();
    rerender(view('success', false)); expect(container.querySelector('[data-status-settled="true"]')).toBeNull();
    expect(screen.getByText('Status')).toBeTruthy();
  });
});
