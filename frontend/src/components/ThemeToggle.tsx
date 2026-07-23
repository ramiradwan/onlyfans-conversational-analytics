import CheckIcon from '@mui/icons-material/Check';
import DarkModeIcon from '@mui/icons-material/DarkMode';
import LightModeIcon from '@mui/icons-material/LightMode';
import MonitorIcon from '@mui/icons-material/Monitor';
import { IconButton, ListItemIcon, ListItemText, Menu, MenuItem, Tooltip } from '@mui/material';
import { useColorScheme } from '@mui/material/styles';
import * as React from 'react';

/** Matches the `Mode` union accepted by MUI's `useColorScheme().setMode`. */
type Mode = 'light' | 'dark' | 'system';

const MODES: readonly { icon: React.ReactNode; label: string; value: Mode }[] = [
  { icon: <LightModeIcon fontSize="small" />, label: 'Light', value: 'light' },
  { icon: <DarkModeIcon fontSize="small" />, label: 'Dark', value: 'dark' },
  { icon: <MonitorIcon fontSize="small" />, label: 'System', value: 'system' },
];

function iconForMode(mode: Mode): React.ReactNode {
  return MODES.find((entry) => entry.value === mode)?.icon ?? <MonitorIcon fontSize="small" />;
}

export function ThemeToggle() {
  const { mode, setMode } = useColorScheme();
  const [mounted, setMounted] = React.useState(false);
  const [anchorEl, setAnchorEl] = React.useState<HTMLElement | null>(null);
  const menuId = React.useId();
  const buttonId = React.useId();
  const open = Boolean(anchorEl);

  React.useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted || !mode) return null;

  const handleOpen = (event: React.MouseEvent<HTMLElement>) => {
    setAnchorEl(event.currentTarget);
  };

  const handleClose = () => {
    setAnchorEl(null);
  };

  const handleSelect = (nextMode: Mode) => {
    setMode(nextMode);
    handleClose();
  };

  return (
    <>
      <Tooltip title="Color mode">
        <IconButton
          id={buttonId}
          aria-controls={open ? menuId : undefined}
          aria-expanded={open ? 'true' : undefined}
          aria-haspopup="menu"
          aria-label={`Color mode: ${mode}. Choose light, dark, or system.`}
          color="inherit"
          onClick={handleOpen}
        >
          {iconForMode(mode)}
        </IconButton>
      </Tooltip>
      <Menu
        id={menuId}
        anchorEl={anchorEl}
        open={open}
        onClose={handleClose}
        slotProps={{ list: { 'aria-labelledby': buttonId, role: 'menu' } }}
      >
        {MODES.map((entry) => (
          <MenuItem
            key={entry.value}
            role="menuitemradio"
            aria-checked={mode === entry.value}
            selected={mode === entry.value}
            onClick={() => handleSelect(entry.value)}
          >
            <ListItemIcon>{entry.icon}</ListItemIcon>
            <ListItemText>{entry.label}</ListItemText>
            {mode === entry.value && (
              <CheckIcon fontSize="small" aria-hidden="true" sx={{ ml: 2 }} />
            )}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}
