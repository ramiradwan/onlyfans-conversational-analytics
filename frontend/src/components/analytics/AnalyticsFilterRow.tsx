import CalendarMonthOutlinedIcon from '@mui/icons-material/CalendarMonthOutlined';
import { Box, Button, Chip, Popover, Stack, TextField, Typography, styled } from '@mui/material';
import { type FormEvent, useEffect, useId, useState } from 'react';

import { type AnalyticsDateRange, formatCalendarDate, formatCalendarRange } from '../../analytics';

const DateFields = styled(Box)(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(1.5),
  gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
}));

const DateField = styled(TextField)({
  '& input::-webkit-calendar-picker-indicator': { display: 'none' },
});

export interface AnalyticsFilterRowProps {
  value: AnalyticsDateRange;
  onApply(range: AnalyticsDateRange): void;
  isRefreshing?: boolean;
}

function rangeLabel(range: AnalyticsDateRange): string {
  if (!range.startDate && !range.endDate) return 'All time';
  if (range.startDate && range.endDate) return formatCalendarRange(range.startDate, range.endDate);
  return range.startDate
    ? `From ${formatCalendarDate(range.startDate)}`
    : `Through ${formatCalendarDate(range.endDate)}`;
}

function inputDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function trailingDays(days: number): AnalyticsDateRange {
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - (days - 1));
  return { startDate: inputDate(start), endDate: inputDate(end) };
}

function sameRange(left: AnalyticsDateRange, right: AnalyticsDateRange): boolean {
  return left.startDate === right.startDate && left.endDate === right.endDate;
}

/** Current date range with a popover for quick ranges and custom dates. */
export function AnalyticsFilterRow({ value, onApply, isRefreshing = false }: AnalyticsFilterRowProps) {
  const [draft, setDraft] = useState(value);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const open = anchor !== null;
  const popoverId = useId();
  const titleId = useId();

  useEffect(() => setDraft(value), [value]);

  const close = () => {
    setDraft(value);
    setAnchor(null);
  };

  const apply = (range: AnalyticsDateRange) => {
    setDraft(range);
    onApply(range);
    setAnchor(null);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    apply(draft);
  };

  const presets = [
    { label: 'Last 30 days', range: trailingDays(30) },
    { label: 'Last 90 days', range: trailingDays(90) },
    { label: 'All time', range: { startDate: '', endDate: '' } },
  ];

  return (
    <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', minWidth: 0 }}>
      <Stack spacing={0.25} sx={{ flex: 1, minWidth: 0 }}>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>Date range</Typography>
        <Typography variant="body2" noWrap>{rangeLabel(value)}</Typography>
      </Stack>
      <Button
        aria-controls={open ? popoverId : undefined}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={(event) => setAnchor(event.currentTarget)}
        size="small"
        startIcon={<CalendarMonthOutlinedIcon />}
        type="button"
        variant="outlined"
      >
        Change dates
      </Button>
      {isRefreshing && (
        <Typography role="status" variant="caption" aria-live="polite" sx={{ color: 'text.secondary' }}>
          Updating…
        </Typography>
      )}

      <Popover
        anchorEl={anchor}
        anchorOrigin={{ horizontal: 'right', vertical: 'bottom' }}
        id={popoverId}
        onClose={close}
        open={open}
        slotProps={{
          paper: {
            'aria-labelledby': titleId,
            role: 'dialog',
            sx: { maxWidth: 'calc(100vw - 32px)', mt: 1, p: 2, width: 360 },
          },
        }}
        transformOrigin={{ horizontal: 'right', vertical: 'top' }}
      >
        <Stack component="form" onSubmit={submit} aria-label="Analytics filters" spacing={2}>
          <Typography component="h2" id={titleId} variant="subtitle2">Show messages from</Typography>
          <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
            {presets.map((preset) => {
              const selected = sameRange(value, preset.range);
              return (
                <Chip
                  aria-pressed={selected}
                  color={selected ? 'primary' : 'default'}
                  key={preset.label}
                  label={preset.label}
                  onClick={() => apply(preset.range)}
                  variant={selected ? 'filled' : 'outlined'}
                />
              );
            })}
          </Stack>
          <DateFields role="group" aria-label="Date range">
            <DateField
              type="date"
              size="small"
              label="Start date"
              value={draft.startDate}
              onChange={(event) => setDraft((current) => ({ ...current, startDate: event.target.value }))}
              slotProps={{ inputLabel: { shrink: true } }}
            />
            <DateField
              type="date"
              size="small"
              label="End date"
              value={draft.endDate}
              onChange={(event) => setDraft((current) => ({ ...current, endDate: event.target.value }))}
              slotProps={{ inputLabel: { shrink: true } }}
            />
          </DateFields>
          <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
            <Button onClick={close} type="button">Cancel</Button>
            <Button type="submit" variant="contained">Apply</Button>
          </Stack>
        </Stack>
      </Popover>
    </Stack>
  );
}
