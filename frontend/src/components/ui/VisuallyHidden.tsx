import { styled } from '@mui/material';

/** Text announced by assistive technology but not rendered visually. */
export const VisuallyHidden = styled('span')({
  clip: 'rect(0 0 0 0)',
  clipPath: 'inset(50%)',
  height: 1,
  overflow: 'hidden',
  position: 'absolute',
  whiteSpace: 'nowrap',
  width: 1,
});
