import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MentionEvidenceDialog } from '../src/components/analytics/MentionEvidenceDialog';
import { findingsResponse, findingsSource, findingsBaselineFixture as baseline } from '../src/story-only/findingsFixtures';
afterEach(cleanup);
it('opens saved context and renders its cited highlight and retained-history indicators', () => {
  const source = findingsSource(findingsResponse().evidence_page.items[0].reference);
  const showContext = vi.fn();
  render(<MentionEvidenceDialog state={{ source, sourceLoading: false, sourceError: null, sourceContext: {
    schema_version: 'analytics-evidence-context.v1', reference: source.reference, projection_generation: baseline.projection_generation,
    projection_digest: baseline.projection_digest, checked_at: source.checked_at, anchor_index: 0,
    items: [{ reference: source.reference, text: source.text, direction: source.direction, browser_span: source.browser_span }],
    has_earlier_stored_items: true, has_later_stored_items: false,
  } }} actions={{ closeSource: vi.fn(), showContext }} />);
  fireEvent.click(screen.getByRole('button', { name: 'Show surrounding messages' }));
  expect(showContext).toHaveBeenCalledOnce();
  const neighborhood = screen.getByLabelText('Surrounding saved messages');
  expect(within(neighborhood).getByText('More earlier saved messages exist.')).toBeTruthy();
  expect(within(neighborhood).getByText(/Cited source/)).toBeTruthy();
  expect(neighborhood.querySelector('mark')?.textContent).toBe(source.text.slice(source.browser_span!.start, source.browser_span!.end));
});
