import MoreHorizOutlined from '@mui/icons-material/MoreHorizOutlined';
import { Box, Button, IconButton, Menu, MenuItem, Typography, type SxProps, type Theme, type TypographyProps } from '@mui/material';
import { useEffect, useId, useState, type ReactNode } from 'react';

import { StatusChip, type StatusTone } from './StatusChip';
import { componentTokens } from '../../theme/generated/tokens';

/** A section's status. A section types its labels with the list it gives its header, so the chip measures each. */
export interface SectionStatus<Label extends string = string> {
  label: Label;
  tone: StatusTone;
}

/** Reports a section's status to its owner, and clears it when the section unmounts. */
export function useReportedStatus(report: ((status: SectionStatus | null) => void) | undefined, status: SectionStatus | null) {
  const label = status?.label ?? null;
  const tone = status?.tone ?? 'default';
  useEffect(() => { report?.(label === null ? null : { label, tone }); }, [report, label, tone]);
  useEffect(() => () => report?.(null), [report]);
}

const tokens = componentTokens.SettingsSection;
// Row controls are small buttons, so a text button's label sits this far inside its box.
const textInset = componentTokens.MuiButton.textInset.small;
const rem = (px: number) => `${px / 16}rem`;
const list = (sx?: SxProps<Theme>) => (Array.isArray(sx) ? sx : [sx]);
const tile = rem(tokens.iconTile);
const gap = rem(tokens.iconGap);

/** Inline-start padding that lets a section icon hang beside the text column. */
export const SETTINGS_SECTION_INDENT = `calc(${tile} + ${gap})`;

/** Width of the status column, shared by fixed-width row actions so both end on one edge. */
export const SETTINGS_STATUS_COLUMN = rem(tokens.statusColumn);

/** Text that keeps the size of the largest copy it can show, so a pushed change moves nothing. It starts where its text starts. */
export function ReservedText({ reserve, text }: { reserve: readonly string[]; text: ReactNode }) {
  return (
    // Start-aligned even inside a button, which would otherwise center each copy differently.
    <Box component="span" sx={{ display: 'grid', textAlign: 'start' }}>
      {reserve.map((copy) => (
        <Box key={copy} component="span" aria-hidden="true" sx={{
          gridArea: '1 / 1', visibility: 'hidden', '&::before': { content: `${JSON.stringify(copy)} / ""` },
        }} />
      ))}
      <Box component="span" sx={{ gridArea: '1 / 1' }}>{text}</Box>
    </Box>
  );
}

/** An informational Alert shown as a quiet note: icon and secondary text without a fill. It keeps the Alert's role. */
export const quietNoteSx: SxProps<Theme> = {
  bgcolor: 'transparent',
  backgroundImage: 'none',
  border: 0,
  borderRadius: 0,
  p: 0,
  color: 'text.secondary',
  '& .MuiAlert-icon': { color: 'text.secondary', mr: 1, py: 0, mt: '0.0625rem', fontSize: '1.125rem' },
  '& .MuiAlert-message': { py: 0 },
};

/** A rare or destructive section action, kept in the header's menu. */
export interface SectionMenuItem {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  destructive?: boolean;
}

function SectionMenu({ items, title }: { items: readonly SectionMenuItem[]; title: string }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const id = useId();
  return (
    <>
      <IconButton
        aria-controls={anchor ? id : undefined}
        aria-expanded={anchor ? 'true' : undefined}
        aria-haspopup="menu"
        aria-label={`${title} options`}
        onClick={(event) => setAnchor(event.currentTarget)}
        size="small"
        sx={{ color: 'text.secondary', flexShrink: 0, p: 0.5 }}
      >
        <MoreHorizOutlined fontSize="small" />
      </IconButton>
      <Menu
        anchorEl={anchor}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        id={id}
        onClose={() => setAnchor(null)}
        open={anchor !== null}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      >
        {items.map((item) => (
          <MenuItem
            disabled={item.disabled}
            key={item.label}
            // A disabled item renders as a list item, which still receives clicks.
            onClick={() => { if (item.disabled) return; setAnchor(null); item.onSelect(); }}
            sx={item.destructive ? { color: 'error.main' } : undefined}
          >
            {item.label}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}

/** Settings card heading: an optional icon, title, summary, status chip announced on change, and a menu of rare actions. */
export function SectionHeader({
  headingProps,
  icon,
  menu,
  status,
  statusLabels,
  sx,
  summary,
  title,
}: {
  headingProps?: Pick<TypographyProps<'h2'>, 'id' | 'ref' | 'sx' | 'tabIndex'>;
  icon?: ReactNode;
  menu?: readonly SectionMenuItem[];
  status?: SectionStatus | null;
  /** Every label the status can show, so the chip keeps one width. */
  statusLabels?: readonly string[];
  sx?: SxProps<Theme>;
  summary?: ReactNode;
  title: string;
}) {
  const { sx: headingSx, ...heading } = headingProps ?? {};
  const hasMenu = Boolean(menu?.length);
  const hasStatus = status !== undefined;
  const stacked = `@container (max-width: ${rem(tokens.stackHeaderBelow)})`;
  // The menu ends the title line in its own column. Beside it, a chip that measures its labels keeps one width.
  const endColumns = [
    ...(hasStatus ? [hasMenu && statusLabels?.length ? 'auto' : `min(50%, ${SETTINGS_STATUS_COLUMN})`] : []),
    ...(hasMenu ? ['auto'] : []),
  ];
  const firstRow = ['title', ...(hasStatus ? ['status'] : []), ...(hasMenu ? ['menu'] : [])];
  const areas = `"${firstRow.join(' ')}" "${firstRow.map(() => 'summary').join(' ')}"`;
  // One title line high, so a chip or the menu centers on the first line of a wrapped title.
  const titleLineBox = (theme: Theme) => ({
    display: 'flex',
    alignItems: 'center',
    minWidth: 0,
    fontSize: theme.typography.h6.fontSize,
    lineHeight: theme.typography.h6.lineHeight,
    blockSize: '1lh',
  });
  // Narrow screens leave the icon out. Inside Settings the card's indent holds it, and elsewhere the title makes room for it.
  const titleIndent = icon ? 'var(--settings-title-indent)' : undefined;
  return (
    <Box data-settings-header sx={{ containerType: 'inline-size' }}>
      <Box data-settings-section-header sx={[(theme) => ({
        ...(icon ? {
          '--settings-title-indent': '0px',
          [theme.breakpoints.up('sm')]: { '--settings-title-indent': `max(0px, calc(${tile} + ${gap} - var(--settings-indent, 0px)))` },
        } : {}),
        display: 'grid',
        gridTemplateColumns: ['minmax(0, 1fr)', ...endColumns].join(' '),
        gridTemplateAreas: areas,
        alignItems: 'start',
        columnGap: 1,
        rowGap: 0.5,
        [stacked]: hasStatus ? {
          gridTemplateColumns: hasMenu ? 'minmax(0, 1fr) auto' : 'minmax(0, 1fr)',
          gridTemplateAreas: hasMenu ? '"title menu" "status status" "summary summary"' : '"title" "status" "summary"',
        } : {},
      }), ...list(sx)]}>
        <Box sx={{
          gridArea: 'title',
          minWidth: 0,
          position: 'relative',
          typography: 'h6',
          paddingInlineStart: titleIndent,
        }}>
          {icon && (
            <Box aria-hidden="true" data-settings-section-icon sx={(theme) => ({
              position: 'absolute',
              insetInlineStart: 'calc(-1 * var(--settings-indent, 0px))',
              top: '0.5lh',
              transform: 'translateY(-50%)',
              boxSizing: 'border-box',
              display: { xs: 'none', sm: 'grid' },
              placeItems: 'center',
              inlineSize: tile,
              blockSize: tile,
              border: `1px solid ${theme.vars.palette.divider}`,
              borderRadius: `${componentTokens.MuiButton.borderRadius}px`,
              bgcolor: 'surface.subtle',
              color: 'text.secondary',
              '& svg': { fontSize: rem(tokens.iconSize) },
            })}>
              {icon}
            </Box>
          )}
          <Typography component="h2" variant="h6" {...heading} sx={[{ overflowWrap: 'anywhere' }, ...list(headingSx)]}>
            {title}
          </Typography>
        </Box>
        {hasStatus && (
          <Box aria-live="polite" sx={(theme) => ({
            ...titleLineBox(theme),
            gridArea: 'status',
            justifyContent: 'flex-end',
            // Stacked, the chip sits under the title, clear of the icon. The indent gives way to a label that needs the room.
            [stacked]: {
              justifyContent: 'flex-start',
              ...(titleIndent ? { '&::before': { content: '""', flex: `0 1 ${titleIndent}`, minWidth: 0 } } : {}),
            },
          })}>
            {status && <StatusChip
              data-settings-status
              label={status.label}
              labels={statusLabels}
              size="small"
              tone={status.tone}
              sx={{ height: '1.5rem', flexShrink: 0 }} />}
          </Box>
        )}
        {hasMenu && (
          <Box sx={(theme) => ({ ...titleLineBox(theme), gridArea: 'menu' })}>
            <SectionMenu items={menu!} title={title} />
          </Box>
        )}
        {summary && (
          <Typography variant="body2" sx={{ gridArea: 'summary', color: 'text.secondary', minWidth: 0, overflowWrap: 'anywhere' }}>
            {summary}
          </Typography>
        )}
      </Box>
    </Box>
  );
}

/** One setting: label and description, with its controls aligned to the end once the row has room. */
export function SettingRow({
  action,
  actionWidth,
  description,
  detail,
  inlineFrom = '26rem',
  reserveActions,
  reserveDescriptions,
  sx,
  title,
  titleLineFrom = '18rem',
}: {
  action?: ReactNode;
  /** Fixed control column beside the text, so a changing control never moves the text. */
  actionWidth?: string;
  description?: ReactNode;
  /** Shown in place of the description when the content brings its own text styles. */
  detail?: ReactNode;
  /** Row width from which the controls sit beside the text instead of below it. */
  inlineFrom?: string;
  /** Every description the row can show. The row keeps the tallest one's height, so a pushed change moves nothing. */
  reserveDescriptions?: readonly string[];
  /** Labels of the buttons the row can show in place of its control. The control cell keeps the widest one's width, so a pushed change never rewraps the text. */
  reserveActions?: readonly string[];
  sx?: SxProps<Theme>;
  title: string;
  /** Row width from which the controls share the title's line, with the description below both. */
  titleLineFrom?: string;
}) {
  // Named, so rules for the title and description measure the row even inside another container.
  const inline = `@container setting-row (min-width: ${inlineFrom})`;
  const titleLine = `@container setting-row (min-width: ${titleLineFrom}) and (not (min-width: ${inlineFrom}))`;
  const endAligned = (inlineSize?: string) => ({
    justifyContent: 'flex-end',
    inlineSize,
    '& > .MuiButton-text:first-of-type': { marginInlineStart: 0 },
    '& > .MuiButton-text:last-of-type': { marginInlineEnd: `-${textInset}px` },
  });
  return (
    <Box data-setting-row sx={[(theme) => ({
      containerType: 'inline-size',
      containerName: 'setting-row',
      borderTop: `1px solid ${theme.vars.palette.divider}`,
      minHeight: rem(tokens.rowMinHeight),
      // Rows stay compact until a wide window has room to spare.
      py: { xs: 1.5, md: 2 },
    }), ...list(sx)]}>
      <Box data-setting-layout sx={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'stretch',
        gap: 1.5,
        // A control reserved on the title's line costs less height than one stacked under the description.
        ...(action ? { [titleLine]: {
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr) auto',
          gridTemplateAreas: '"title action" "description description"',
          alignItems: 'center',
          columnGap: 3,
          rowGap: 0,
          '& > [data-setting-text]': { display: 'contents' },
          '& [data-setting-title]': { gridArea: 'title' },
          '& [data-setting-description]': { gridArea: 'description' },
          '& > [data-setting-action]': { gridArea: 'action' },
        } } : {}),
        [inline]: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 3 },
      }}>
        <Box data-setting-text sx={{ minWidth: 0, flex: '1 1 auto' }}>
          <Typography component="h3" data-setting-title variant="subtitle2">
            {title}
          </Typography>
          {detail ? <Box data-setting-description sx={{ minWidth: 0 }}>{detail}</Box> : description && (
            <Typography data-setting-description variant="body2" sx={{ color: 'text.secondary' }}>
              {reserveDescriptions?.length ? <ReservedText reserve={reserveDescriptions} text={description} /> : description}
            </Typography>
          )}
        </Box>
        {action && (
          <Box data-setting-action sx={{
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            justifyContent: 'flex-start',
            gap: 1,
            flexShrink: 0,
            maxInlineSize: '100%',
            // Every control cell has one height, so a switch, a button or nothing gives the row the same shape.
            minBlockSize: '2rem',
            '& .MuiButton-root': { minHeight: '2rem' },
            // A text button's label, not its padding, sits on the shared edge.
            '& > .MuiButton-text:first-of-type': { marginInlineStart: `-${textInset}px` },
            [titleLine]: endAligned(actionWidth),
            [inline]: endAligned(actionWidth),
          }}>
            {reserveActions?.length ? (
              <Box sx={{ display: 'grid', alignItems: 'center', justifyItems: 'start',
                [titleLine]: { justifyItems: 'end' }, [inline]: { justifyItems: 'end' } }}>
                {reserveActions.map((label) => (
                  <Button aria-hidden disabled key={label} size="small" tabIndex={-1} variant="outlined"
                    sx={{ gridArea: '1 / 1', visibility: 'hidden' }}>{label}</Button>
                ))}
                {/* Fills the reserved cell, so trading a switch for a button moves nothing around it. */}
                <Box sx={{ gridArea: '1 / 1', alignSelf: 'stretch', justifySelf: 'stretch', display: 'flex', alignItems: 'center',
                  [titleLine]: { justifyContent: 'flex-end' }, [inline]: { justifyContent: 'flex-end' } }}>{action}</Box>
              </Box>
            ) : action}
          </Box>
        )}
      </Box>
    </Box>
  );
}
