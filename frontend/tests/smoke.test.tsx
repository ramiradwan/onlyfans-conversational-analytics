import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

afterEach(() => cleanup());

describe('React test harness', () => {
  it('renders content in the test DOM', () => {
    render(<main>Bridge test harness ready</main>);

    expect(screen.getByRole('main').textContent).toBe('Bridge test harness ready');
  });
});
