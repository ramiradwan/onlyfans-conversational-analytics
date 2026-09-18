import { keyframes, type CSSObject } from '@emotion/react';

import { effectTokens } from './generated/tokens';

const { duration, easing, keyframes: frames } = effectTokens.motion;
const reducedMotion = { '@media (prefers-reduced-motion: reduce)': { animation: 'none' } };
const rise = keyframes(frames.surfaceArrival);
const grow = keyframes(frames.barArrival);
const reveal = keyframes(frames.lineArrival);
const pop = keyframes(frames.pointArrival);
const settled = keyframes(frames.statusSettled);

/** One mount-time arrival. Delays plus duration never exceed the spatial budget. */
export function surfaceArrival(step = 0): CSSObject {
  const delay = Math.min(2, Math.max(0, step)) * Number.parseInt(duration.fast, 10) / 3;
  return { animation: `${rise} ${duration.standard} ${easing.enter} ${delay}ms both`, ...reducedMotion };
}

export const barArrival: CSSObject = {
  animation: `${grow} ${duration.spatial} ${easing.enter} both`, transformOrigin: 'left center', ...reducedMotion,
};
export const lineArrival: CSSObject = { animation: `${reveal} ${duration.standard} ${easing.enter} both`, ...reducedMotion };
export const pointArrival: CSSObject = {
  animation: `${pop} ${duration.fast} ${easing.enter} ${duration.standard} both`, ...reducedMotion,
};
export const statusSettled: CSSObject = {
  animation: `${settled} ${duration.standard} ${easing.standard} both`, ...reducedMotion,
};