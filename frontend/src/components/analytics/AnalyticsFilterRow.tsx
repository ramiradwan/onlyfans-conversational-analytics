import CalendarMonthOutlinedIcon from '@mui/icons-material/CalendarMonthOutlined';
import FilterAltOutlinedIcon from '@mui/icons-material/FilterAltOutlined';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import { Button, Collapse, Stack, TextField, Typography, styled } from '@mui/material';
import { type FormEvent, useEffect, useState } from 'react';

import type { AnalyticsDateRange } from '../../analytics';

const FilterForm = styled('form')(({ theme }) => ({
  display: 'grid',
  gap: theme.spacing(1.5),
}));

const DateGroup = styled(Stack)(({ theme }) => ({
  alignItems: 'flex-end',
  flexDirection: 'row',
  flexWrap: 'wrap',
  gap: theme.spacing(1),
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
  if (range.startDate && range.endDate) return `${range.startDate} – ${range.endDate}`;
  return range.startDate ? `From ${range.startDate}` : `Through ${range.endDate}`;
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

export function AnalyticsFilterRow({ value, onApply, isRefreshing = false }: AnalyticsFilterRowProps) {
  const [draft, setDraft] = useState(value);
  const [showDates, setShowDates] = useState(false);

  useEffect(() => setDraft(value), [value]);

  const apply = (range: AnalyticsDateRange) => {
    setDraft(range);
    onApply(range);
    setShowDates(false);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    apply(draft);
  };

  const clear = () => apply({ startDate: '', endDate: '' });

  return (
    <FilterForm onSubmit={submit} aria-label="Analytics filters">
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', minWidth: 0 }}>
        <Stack spacing={0.25} sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>Dates</Typography>
          <Typography variant="body2" noWrap>{rangeLabel(value)}</Typography>
        </Stack>
        <Button
          aria-expanded={showDates}
          onClick={() => setShowDates((open) => !open)}
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
      </Stack>

      <Collapse in={showDates}>
        <Stack spacing={1.5} sx={{ pt: 0.5 }}>
          <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap' }}>
            <Button type="button" size="small" onClick={() => apply(trailingDays(30))}>Last 30 days</Button>
            <Button type="button" size="small" onClick={() => apply(trailingDays(90))}>Last 90 days</Button>
            <Button type="button" size="small" startIcon={<RestartAltIcon />} onClick={clear}>All time</Button>
          </Stack>
          <DateGroup aria-label="Date range">
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
            <Button type="submit" variant="contained" startIcon={<FilterAltOutlinedIcon />}>Apply</Button>
          </DateGroup>
        </Stack>
      </Collapse>
    </FilterForm>
  );
}
