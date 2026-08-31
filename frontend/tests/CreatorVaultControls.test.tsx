import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CreatorVaultControls } from '../src/components/CreatorVaultControls';
import type {
  CreatorVaultApi,
  CreatorVaultCommand,
  CreatorVaultCommandResult,
  CreatorVaultExportDocument,
  CreatorVaultStatus,
} from '../src/services/creatorVaultApi';
import { useUserStore } from '../src/store/userStore';
import { theme } from '../src/theme';

const disabled: CreatorVaultStatus = {
  creator_account_id: 'creator-1',
  policy: {
    enabled: false,
    policy_type: 'disabled',
    finite_horizon_days: null,
    revision: 0,
  },
  capabilities: {
    finite_retention: true,
    indefinite_retention: false,
    deletion_scopes: ['message', 'conversation', 'participant', 'all'],
    unlink_archive_treatments: ['preserve', 'delete'],
    export: true,
  },
};

const enabled: CreatorVaultStatus = {
  ...disabled,
  policy: {
    enabled: true,
    policy_type: 'finite',
    finite_horizon_days: 365,
    revision: 1,
  },
};

const exportDocument: CreatorVaultExportDocument = {
  manifest: {
    export_type: 'creator_vault',
    content: {
      conversation_count: 1,
      message_count: 1,
      sha256: 'sha256:test',
    },
    copy_domains: {
      managed_recovery: {
        inspection_complete: false,
        copies_may_remain: true,
      },
      this_export_after_delivery: {
        managed_by_product: false,
        observable_by_product: false,
        managed_vault_deletion_applies: false,
      },
    },
  },
  conversations: [{ conversation_id: 'chat-1' }],
  messages: [{ message_id: 'message-1' }],
};

function result(
  input: CreatorVaultCommand,
  status: CreatorVaultStatus,
): CreatorVaultCommandResult {
  return {
    action: input.action,
    status,
    deletion_revision: input.action.startsWith('delete_') ? 2 : null,
    unlink_archive_treatment: input.action === 'unlink'
      ? input.unlink_archive_treatment ?? null
      : null,
  };
}

beforeEach(() => {
  useUserStore.getState().actions.setUserRole('creator-ceo');
});

afterEach(() => {
  cleanup();
  useUserStore.getState().actions.setUserRole(null);
});

describe('CreatorVaultControls', () => {
  it('exercises finite enable, deletion, unlink treatment, export, and disable through the API', async () => {
    const command = vi.fn(async (input: CreatorVaultCommand) => (
      result(input, input.action === 'disable' ? disabled : enabled)
    ));
    const api: CreatorVaultApi = {
      get: vi.fn(async () => disabled),
      command,
      exportDocument: vi.fn(async () => exportDocument),
    };
    const onDownload = vi.fn();

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <CreatorVaultControls api={api} onDownload={onDownload} />
      </ThemeProvider>,
    );

    expect(await screen.findByText('Disabled')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Keep until I delete' })).toBeNull();

    fireEvent.change(screen.getByLabelText('Retention days'), {
      target: { value: '365' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Enable finite Vault' }));
    await waitFor(() => expect(command).toHaveBeenCalledWith({
      action: 'enable_finite',
      finite_horizon_days: 365,
    }));
    expect(await screen.findByRole('button', { name: 'Disable Vault' })).toBeTruthy();

    fireEvent.change(screen.getByLabelText('Message ID'), {
      target: { value: 'message-42' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Delete selected scope' }));
    await waitFor(() => expect(command).toHaveBeenCalledWith({
      action: 'delete_message',
      target_id: 'message-42',
    }));

    fireEvent.click(screen.getByRole('button', { name: 'Delete all Vault data' }));
    await waitFor(() => expect(command).toHaveBeenCalledWith({ action: 'delete_all' }));

    fireEvent.click(screen.getByRole('button', { name: 'Apply unlink treatment' }));
    await waitFor(() => expect(command).toHaveBeenCalledWith({
      action: 'unlink',
      unlink_archive_treatment: 'preserve',
    }));

    fireEvent.click(screen.getByRole('button', { name: 'Export Creator Vault' }));
    await waitFor(() => expect(api.exportDocument).toHaveBeenCalledTimes(1));
    expect(onDownload).toHaveBeenCalledWith(exportDocument);
    expect(await screen.findByText(/recovery copies may still remain/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Disable Vault' }));
    await waitFor(() => expect(command).toHaveBeenCalledWith({ action: 'disable' }));
    expect(await screen.findByText('Disabled')).toBeTruthy();
  });

  it('shows the indefinite option only when the backend capability permits it', async () => {
    const api: CreatorVaultApi = {
      get: vi.fn(async () => ({
        ...disabled,
        capabilities: {
          ...disabled.capabilities,
          indefinite_retention: true,
        },
      })),
      command: vi.fn(async (input) => result(input, {
        ...enabled,
        policy: {
          enabled: true,
          policy_type: 'indefinite_until_delete',
          finite_horizon_days: null,
          revision: 1,
        },
      })),
      exportDocument: vi.fn(async () => exportDocument),
    };

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <CreatorVaultControls api={api} onDownload={vi.fn()} />
      </ThemeProvider>,
    );

    const indefinite = await screen.findByRole('button', { name: 'Keep until I delete' });
    fireEvent.click(indefinite);
    await waitFor(() => expect(api.command).toHaveBeenCalledWith({
      action: 'enable_indefinite',
    }));
  });

  it('withholds creator-only Vault state and mutations from operators', async () => {
    useUserStore.getState().actions.setUserRole('operator');
    const api: CreatorVaultApi = {
      get: vi.fn(async () => disabled),
      command: vi.fn(),
      exportDocument: vi.fn(async () => exportDocument),
    };

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <CreatorVaultControls api={api} onDownload={vi.fn()} />
      </ThemeProvider>,
    );

    expect(screen.getByText(/available only to the creator account owner/)).toBeTruthy();
    expect(api.get).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Enable finite Vault' })).toBeNull();
  });
});
