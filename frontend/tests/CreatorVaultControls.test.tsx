import { ThemeProvider } from '@mui/material/styles';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  it('exercises finite enable, confirmed deletion, export, and confirmed disable through the API', async () => {
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

    expect(await screen.findByText('Off')).toBeTruthy();
    expect(screen.queryByLabelText('Days to keep')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Turn on archive' }));
    expect(screen.queryByRole('button', { name: 'Keep until I delete' })).toBeNull();
    expect(screen.queryByText(/removed automatically/)).toBeNull();
    expect(screen.queryByLabelText(/ ID$/)).toBeNull();

    fireEvent.change(screen.getByLabelText('Days to keep'), {
      target: { value: '365' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Keep messages' }));
    await waitFor(() => expect(command).toHaveBeenCalledWith({
      action: 'enable_finite',
      finite_horizon_days: 365,
    }));
    expect(await screen.findByRole('button', { name: 'Turn off archive' })).toBeTruthy();
    expect(screen.getByText('Keeping messages for 365 days')).toBeTruthy();
    expect(screen.queryByLabelText('Days to keep')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Delete messages' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete all messages' }));
    expect(command).not.toHaveBeenCalledWith({ action: 'delete_all' });
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete all' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(command).toHaveBeenCalledWith({ action: 'delete_all' }));
    expect(await screen.findByText('Messages deleted.')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Download messages' }));
    await waitFor(() => expect(api.exportDocument).toHaveBeenCalledTimes(1));
    expect(onDownload).toHaveBeenCalledWith(exportDocument);
    expect(await screen.findByText(/Some backup copies may stay on this computer/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Turn off archive' }));
    expect(command).not.toHaveBeenCalledWith({ action: 'disable' });
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Turn off' }));
    await waitFor(() => expect(command).toHaveBeenCalledWith({ action: 'disable' }));
    expect(await screen.findByText('Off')).toBeTruthy();
  });

  it('surfaces incomplete managed deletion and retries dependent cleanup', async () => {
    const incompleteStatus: CreatorVaultStatus = {
      ...enabled,
      deletion_operation: {
        operation_id: 'operation-1',
        status: 'incomplete',
        deletion_revision: 2,
      },
    };
    const command = vi.fn(async (input: CreatorVaultCommand) => ({
      ...result(input, incompleteStatus),
      deletion_operation: incompleteStatus.deletion_operation,
    }));
    const retryDeletion = vi.fn(async () => ({
      operation_id: 'operation-1',
      status: 'complete' as const,
      deletion_revision: 2,
    }));
    const api: CreatorVaultApi = {
      get: vi.fn(async () => enabled),
      command,
      retryDeletion,
      exportDocument: vi.fn(async () => exportDocument),
    };

    render(
      <ThemeProvider theme={theme} defaultMode="light">
        <CreatorVaultControls api={api} onDownload={vi.fn()} />
      </ThemeProvider>,
    );

    expect(await screen.findByText('Keeping messages for 365 days')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Delete messages' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete all messages' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete all' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(await screen.findByText("Deleting isn't finished")).toBeTruthy();
    expect(screen.queryByText('Messages deleted.')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Finish deleting' }));
    await waitFor(() => expect(retryDeletion).toHaveBeenCalledWith('operation-1'));
    expect(await screen.findByText('Messages deleted.')).toBeTruthy();
    expect(screen.queryByText("Deleting isn't finished")).toBeNull();
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

    fireEvent.click(await screen.findByRole('button', { name: 'Turn on archive' }));
    const indefinite = screen.getByRole('button', { name: 'Keep until I delete' });
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

    expect(screen.getByText('Only the account owner can manage stored messages.')).toBeTruthy();
    expect(api.get).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Keep messages' })).toBeNull();
  });
});
