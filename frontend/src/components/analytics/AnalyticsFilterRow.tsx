import FilterAltOutlinedIcon from '@mui/icons-material/FilterAltOutlined';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import {
  Button,
  Stack,
  TextField,
  Typography,
  styled,
} from '@mui/material';
import { type FormEvent, useEffect, useState } from 'react';

import type { AnalyticsDateRange } from '../../analytics';

const FilterForm = styled('form')(({ theme }) => ({
  alignItems: 'flex-end',
  backgroundColor: theme.vars.palette.background.paper,
  display: 'flex',
  flexWrap: 'wrap',
  gap: theme.spacing(1.5),
  padding: theme.spacing(1.5, 2),
  ...theme.effects.cardBorder(theme),
}));

const DateGroup = styled(Stack)(({ theme }) => ({
  alignItems: 'flex-end',
  flexDirection: 'row',
  flexWrap: 'wrap',
  gap: theme.spacing(1),
}));

const DateField = styled(TextField)({
  '& input::-webkit-calendar-picker-indicator': {
    display: 'none',
  },
});

export interface AnalyticsFilterRowProps {
  value: AnalyticsDateRange;
  onApply(range: AnalyticsDateRange): void;
  isRefreshing?: boolean;
}

export function AnalyticsFilterRow({
  value,
  onApply,
  isRefreshing = false,
}: AnalyticsFilterRowProps) {
  const [draft, setDraft] = useState(value);

  useEffect(() => setDraft(value), [value]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onApply(draft);
  };

  const clear = () => {
    const empty = { startDate: '', endDate: '' };
    setDraft(empty);
    onApply(empty);
  };

  return (
    <FilterForm onSubmit={submit} aria-label="Analytics filters">
      <DateGroup aria-label="Date range">
        <Typography component="p" variant="subtitle2">Requested date range</Typography>
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
      </DateGroup>
      <Stack direction="row" spacing={1}>
        <Button type="submit" variant="contained" startIcon={<FilterAltOutlinedIcon />}>
          Apply
        </Button>
        <Button type="button" variant="text" startIcon={<RestartAltIcon />} onClick={clear}>
          All time
        </Button>
      </Stack>
      {isRefreshing && (
        <Typography role="status" variant="caption" aria-live="polite" sx={{
          color: 'text.secondary'
        }}>
          Refreshing while the prior frame remains visible…
        </Typography>
      )}
    </FilterForm>
  );
}
