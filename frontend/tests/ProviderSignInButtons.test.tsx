import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { ProviderSignInButtons } from '../src/components/ProviderSignInButtons';

afterEach(cleanup);

it('shows only configured providers with official named controls and decorative logos', () => {
  const select = vi.fn();
  render(<><p id="sign-in-guidance">Continue in Chrome.</p><ProviderSignInButtons
    providers={['google', 'microsoft-personal', 'default']} disabled={false}
    onSelect={select} describedBy="sign-in-guidance" /></>);
  expect(select).not.toHaveBeenCalled();
  const buttons = screen.getAllByRole('button');
  expect(buttons.map(button => button.textContent)).toEqual(['Continue with Google', 'Continue with Microsoft', 'Other sign-in options']);
  for (const button of buttons) {
    expect(button.getAttribute('type')).toBe('button');
    expect(button.getAttribute('aria-describedby')).toBe('sign-in-guidance');
    fireEvent.click(button);
  }
  expect(select.mock.calls).toEqual([['google'], ['microsoft-personal'], ['default']]);
  for (const image of document.querySelectorAll('img')) expect(image.alt).toBe('');
});

it('does not render unavailable provider choices or dispatch a disabled choice', () => {
  const select = vi.fn();
  render(<ProviderSignInButtons providers={['default']} disabled onSelect={select} describedBy="status" />);
  expect(screen.queryByRole('button', { name: 'Continue with Google' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Continue with Microsoft' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Other sign-in options' }));
  expect(select).not.toHaveBeenCalled();
});
