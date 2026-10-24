// @vitest-environment jsdom
import { webcrypto } from 'node:crypto';
import { ThemeProvider } from '@mui/material';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { LiteralsClient } from '../src/analytics/literalsClient';
import { parseLiterals } from '../src/analytics/literalsContract';
import { LiteralFindings } from '../src/components/analytics/LiteralFindings';
import { createLiteralsStore, type LiteralsStore } from '../src/store/literalsStore';
import { findingsBaselineFixture as baseline, findingsSource } from '../src/story-only/findingsFixtures';
import { literalsResponse } from '../src/story-only/literalsFixtures';
import { theme } from '../src/theme';
import { context } from './questionFixture';

let store: LiteralsStore;
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
afterEach(() => { cleanup(); store?.actions.dispose(); vi.unstubAllGlobals(); });
function setup(overrides: Partial<LiteralsClient> = {}) {
  const api: LiteralsClient = { read: vi.fn(async (r) => parseLiterals(literalsResponse(r), r)), evidence: vi.fn(async (r) => findingsSource(r)), ...overrides };
  store = createLiteralsStore(api); const state = context();
  render(<ThemeProvider theme={theme}><LiteralFindings baseline={baseline} direction="all" conversation={null} controller={store}
    transport={{ getState: () => state, subscribe: () => () => undefined }} /></ThemeProvider>);
  return api;
}
describe('LiteralFindings', () => {
  it('loads on demand, preserves spelling safely and separates normalization coverage', async () => {
    const api = setup(); expect(api.read).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Show recurring literal values' }));
    const table = await screen.findByRole('table', { name: 'Recurring literal values' });
    expect(within(table).getByText('item')).toBeTruthy(); expect(within(table).getByText('<img>')).toBeTruthy();
    expect(table.querySelector('img')).toBeNull();
    expect(screen.getByText(/Normalization coverage.*Unrepresentable: 1/)).toBeTruthy();
    expect(screen.getByText(/Extraction coverage.*Complete: 2.*Partial: 1/)).toBeTruthy();
    expect(screen.getByText(/No prevalence percentage is inferred/)).toBeTruthy();
  });
  it('selects and clears literal sources without changing the ranked values and opens exact highlights', async () => {
    const api = setup(); fireEvent.click(screen.getByRole('button', { name: 'Show recurring literal values' }));
    fireEvent.click(await screen.findByRole('button', { name: 'View sources for literal item (synthetic-mention)' }));
    await screen.findByRole('button', { name: 'Clear literal selection' });
    expect(screen.getByRole('table', { name: 'Recurring literal values' }).textContent).toContain('<img>');
    expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ node_id: `sha256:${'2'.repeat(64)}`, evidence_offset: 0, expected_normalization_sha256: `sha256:${'4'.repeat(64)}` }), expect.any(AbortSignal));
    fireEvent.click(screen.getByRole('button', { name: 'Literal source 1: synthetic-mention (complete)' }));
    const dialog = await screen.findByRole('dialog', { name: 'Finding source' });
    expect(dialog.querySelector('mark')?.textContent).toBe('item'); expect(dialog.querySelector('img')).toBeNull();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: 'Clear literal selection' }));
    await waitFor(() => expect(api.read).toHaveBeenLastCalledWith(expect.objectContaining({ node_id: null, evidence_offset: 0 }), expect.any(AbortSignal)));
    expect(await screen.findByRole('button', { name: 'Literal source 3: synthetic-mention (complete)' })).toBeTruthy();
  });
  it('shows retained extraction coverage when normalization is not prepared', async () => {
    setup({ read: async (r) => {
      const value = literalsResponse(r); value.availability = 'unavailable'; value.unavailable_reason = 'not_prepared';
      value.normalization_sha256 = null; value.normalization_policy = null; value.normalization_coverage = null;
      value.row_page = { ...value.row_page, items: [], total: 0, has_more: false };
      value.evidence_page = { ...value.evidence_page, items: [], total: 0, has_more: false };
      return parseLiterals(value, r);
    } });
    fireEvent.click(screen.getByRole('button', { name: 'Show recurring literal values' }));
    expect(await screen.findByText(/No verified literal grouping is available/)).toBeTruthy();
    expect(screen.getByText(/Extraction coverage/)).toBeTruthy();
    expect(screen.queryByRole('table', { name: 'Recurring literal values' })).toBeNull();
  });
});
