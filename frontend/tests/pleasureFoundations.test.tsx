import fs from 'node:fs';
import Color from 'colorjs.io';
import { ThemeProvider } from '@mui/material/styles';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { DashboardOverview } from '../src/components/dashboard/DashboardOverview';
import { Panel } from '../src/components/ui/Panel';
import { SectionHeader } from '../src/components/ui/SettingsSection';
import { theme } from '../src/theme';
import { semanticIntents, typography, brandPalette, effectTokens } from '../src/theme/generated/tokens';
import { contrastRatio } from '../src/theme/color-validation';

afterEach(cleanup);
const hex = (color: string) => new Color(color).to('srgb').toString({ format: 'hex' });

describe('Pleasure Pass foundations', () => {
  it('uses the proposed light measurement and warm canvas through intent roles', () => {
    expect(hex(semanticIntents.light.measurement.main)).toBe('#0f7a72');
    expect(hex(semanticIntents.light.surface.canvas)).toBe('#f6f4f1');
    expect(hex(semanticIntents.light.text.primary)).toBe('#191b1a');
    expect(semanticIntents.light.action.primary).toEqual(semanticIntents.light.measurement);
  });
  it.each(['light', 'dark'] as const)('keeps %s measurement and action foregrounds readable', (mode) => {
    const intent = semanticIntents[mode];
    expect(contrastRatio(intent.measurement.main, intent.surface.paper)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(intent.action.primary.contrastText, intent.action.primary.main)).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(intent.text.secondary, intent.communication.outgoingSurface)).toBeGreaterThanOrEqual(4.5);
  });
  it('keeps the brand violet instead of silently recoloring the mark', () => {
    expect(semanticIntents.light.brand.main).toBe(brandPalette.groundedTech.primary);
    expect(semanticIntents.dark.brand.main).toBe(brandPalette.groundedTech.light);
    expect(semanticIntents.light.brand.main).not.toBe(semanticIntents.light.action.primary.main);
    const mark = fs.readFileSync('src/layouts/BrandMark.tsx', 'utf8');
    expect(mark).toContain('theme.vars.palette.brand.main');
    expect(mark).not.toContain('theme.vars.palette.primary');
  });
  it('bundles the display face in application and design previews, for declared numeric and passkey title roles', () => {
    const css = fs.readFileSync('src/index.css', 'utf8');
    const config = JSON.parse(fs.readFileSync('.design-sync/config.json', 'utf8'));
    const font = '@fontsource-variable/space-grotesk/wght.css';
    expect(css).toContain(`@import '${font}'`);
    expect(config.extraFonts).toContain('node_modules/' + font);
    expect(fs.readFileSync('node_modules/' + font, 'utf8')).toContain("font-family: 'Space Grotesk Variable'");
    expect(typography.kpi.fontFamily).toContain('Space Grotesk Variable');
    expect(typography.metric.fontFamily).toContain('Space Grotesk Variable');
    expect(typography.fontFamily).not.toContain('Space Grotesk');
    for (const [role, definition] of Object.entries(typography)) {
      if (!['kpi', 'metric', 'insight', 'numericCaption', 'numericBody', 'passkeyTitle'].includes(role)) expect(JSON.stringify(definition)).not.toContain('Space Grotesk');
    }
    expect(css).not.toMatch(/@import\s+.*https?:/);
    expect(effectTokens.motion.duration).toEqual({ fast: '120ms', standard: '200ms', spatial: '320ms' });
  });
  it('preserves panel attributes and callback/array styling on the existing primitive', () => {
    render(<ThemeProvider theme={theme}>
      <Panel component="section" aria-label="Overview" emphasis="dominant"
        sx={[(t) => ({ p: '32px', color: t.vars.palette.text.primary }), { gap: 1 }]}>
        Content
      </Panel>
    </ThemeProvider>);
    const panel = screen.getByRole('region', { name: 'Overview' });
    expect(panel.getAttribute('data-surface-emphasis')).toBe('dominant');
    expect(panel.textContent).toBe('Content');
    expect(getComputedStyle(panel).paddingTop).toBe('32px');
  });
  it('keeps missing dashboard data unknown and progress indeterminate', () => {
    render(<ThemeProvider theme={theme}>
      <DashboardOverview conversations="—" messages="—" received="—" sent="—"
        progress={{ label: 'Reading history', percent: null }} />
    </ThemeProvider>);
    expect(screen.getByRole('region', { name: 'Overview' }).textContent).not.toMatch(/\b0\b/);
    expect(screen.getByRole('group', { name: 'Conversations' }).textContent).toContain('—');
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBeNull();
  });
  it('retains the status live region across feedback changes', () => {
    const view = (label: string, tone: 'info' | 'success') => (
      <ThemeProvider theme={theme}>
        <SectionHeader title="Stored messages" status={{ label, tone }} />
      </ThemeProvider>
    );
    const { container, rerender } = render(view('Updating', 'info'));
    expect(container.querySelector('[aria-live="polite"]')?.textContent).toBe('Updating');
    rerender(view('Up to date', 'success'));
    expect(container.querySelector('[aria-live="polite"]')?.textContent).toBe('Up to date');
  });
});
