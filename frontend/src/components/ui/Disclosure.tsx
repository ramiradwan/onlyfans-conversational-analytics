import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { Box, Button, Collapse } from '@mui/material';
import { useId, useState } from 'react';

export interface DisclosureProps {
  children: React.ReactNode;
  defaultOpen?: boolean;
  label: string;
}

/** Secondary content hidden behind a single toggle until the viewer asks for it. */
export const Disclosure: React.FC<DisclosureProps> = ({ children, defaultOpen = false, label }) => {
  const [open, setOpen] = useState(defaultOpen);
  const regionId = useId();
  return (
    <Box>
      <Button
        aria-controls={regionId}
        aria-expanded={open}
        endIcon={(
          <ExpandMoreIcon
            sx={{
              transform: open ? 'rotate(180deg)' : 'none',
              transition: (theme) => theme.transitions.create('transform', { duration: 200 }),
            }}
          />
        )}
        onClick={() => setOpen((value) => !value)}
        size="small"
        sx={{ ml: -1 }}
      >
        {label}
      </Button>
      <Collapse id={regionId} in={open}>
        <Box sx={{ pt: 1.5 }}>{children}</Box>
      </Collapse>
    </Box>
  );
};
