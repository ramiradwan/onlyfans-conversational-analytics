import { act, cleanup, render, screen } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RevealGroup, useRevealHold } from '../src/components/ui';

function Section({ label, pending }: { label: string; pending: boolean }) {
  useRevealHold(pending);
  return <p>{pending ? `${label} loading` : `${label} ready`}</p>;
}

function Group({ first, second, maxWaitMs }: { first: boolean; second: boolean; maxWaitMs?: number }) {
  return (
    <RevealGroup fallback={<p role="status">Waiting</p>} maxWaitMs={maxWaitMs}>
      <Section label="First" pending={first} />
      <Section label="Second" pending={second} />
    </RevealGroup>
  );
}

function shown(text: string): boolean {
  const node = screen.queryByText(text);
  return node !== null && node.closest('[hidden]') === null;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('RevealGroup', () => {
  it('keeps every section hidden until the last one settles', () => {
    const { rerender } = render(<Group first second />);
    expect(shown('Waiting')).toBe(true);
    expect(shown('First loading')).toBe(false);

    rerender(<Group first={false} second />);
    expect(shown('Waiting')).toBe(true);
    expect(shown('First ready')).toBe(false);

    rerender(<Group first={false} second={false} />);
    expect(screen.queryByRole('status')).toBeNull();
    expect(shown('First ready')).toBe(true);
    expect(shown('Second ready')).toBe(true);
  });

  it('keeps sections hidden while strict mode remounts their effects', () => {
    render(<StrictMode><Group first second /></StrictMode>);
    expect(shown('Waiting')).toBe(true);
    expect(shown('First loading')).toBe(false);
  });

  it('reveals at once when no section is loading', () => {
    render(<Group first={false} second={false} />);
    expect(screen.queryByRole('status')).toBeNull();
    expect(shown('Second ready')).toBe(true);
  });

  it('stays revealed when a section loads again', () => {
    const { rerender } = render(<Group first={false} second={false} />);
    rerender(<Group first second={false} />);
    expect(screen.queryByRole('status')).toBeNull();
    expect(shown('First loading')).toBe(true);
  });

  it('reveals after the time limit when a section never settles', () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    render(<Group first second={false} maxWaitMs={500} />);
    expect(shown('First loading')).toBe(false);

    act(() => vi.advanceTimersByTime(500));

    expect(screen.queryByRole('status')).toBeNull();
    expect(shown('First loading')).toBe(true);
  });

  it('leaves sections outside a group visible', () => {
    render(<Section label="Alone" pending />);
    expect(shown('Alone loading')).toBe(true);
  });
});
