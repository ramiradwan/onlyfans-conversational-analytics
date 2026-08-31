"""Authenticated Creator Vault lifecycle controls."""
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.activation import require_activated_runtime
from app.api.security import (
    get_authenticated_runtime_policy,
    require_creator,
    verify_same_origin,
    verify_csrf_token,
)
from app.persistence.archive_export import CreatorVaultExporter
from app.persistence.deletion_operations import (
    ManagedDeletionOperation,
    ManagedDeletionOperations,
)
from app.persistence.retention import (
    CreatorVaultRetention,
    RetentionPolicyError,
    production_indefinite_gate_open,
)
from app.security.runtime_policy import RuntimePolicy
from app.transport.manager import transport_manager


router = APIRouter(
    prefix="/api/v1/settings/creator-vault",
    tags=["Creator Vault"],
    dependencies=[Depends(require_activated_runtime)],
)

CommandAction = Literal[
    "enable_finite",
    "enable_indefinite",
    "disable",
    "delete_message",
    "delete_conversation",
    "delete_participant",
    "delete_all",
    "unlink",
]
UnlinkArchiveTreatment = Literal["preserve", "delete"]


class VaultPolicyResponse(BaseModel):
    enabled: bool
    policy_type: Literal[
        "disabled", "finite", "export_and_delete", "indefinite_until_delete"
    ]
    finite_horizon_days: int | None
    revision: int


class VaultDeletionOperationResponse(BaseModel):
    operation_id: str
    status: Literal["pending", "incomplete", "complete"]
    deletion_revision: int


class VaultCapabilitiesResponse(BaseModel):
    finite_retention: bool = True
    indefinite_retention: bool
    deletion_scopes: list[Literal["message", "conversation", "participant", "all"]]
    unlink_archive_treatments: list[UnlinkArchiveTreatment]
    export: bool = True


class VaultStatusResponse(BaseModel):
    creator_account_id: str
    policy: VaultPolicyResponse
    capabilities: VaultCapabilitiesResponse
    deletion_operation: VaultDeletionOperationResponse | None = None


class VaultCommandRequest(BaseModel):
    action: CommandAction
    finite_horizon_days: int | None = Field(default=None, gt=0)
    target_id: str | None = None
    unlink_archive_treatment: UnlinkArchiveTreatment | None = None


class VaultCommandResponse(BaseModel):
    action: CommandAction
    status: VaultStatusResponse
    deletion_revision: int | None = None
    deletion_operation: VaultDeletionOperationResponse | None = None
    unlink_archive_treatment: UnlinkArchiveTreatment | None = None


def _retention() -> CreatorVaultRetention:
    return CreatorVaultRetention(transport_manager.canonical_database)


def _deletions() -> ManagedDeletionOperations:
    return ManagedDeletionOperations(transport_manager.canonical_database)


def _operation_response(operation: ManagedDeletionOperation) -> VaultDeletionOperationResponse:
    return VaultDeletionOperationResponse(
        operation_id=operation.operation_id,
        status=operation.status,
        deletion_revision=operation.deletion_revision,
    )


def _status(account_id: str) -> VaultStatusResponse:
    policy = _retention().policy(account_id)
    outstanding = _deletions().outstanding(account_id)
    return VaultStatusResponse(
        creator_account_id=account_id,
        policy=VaultPolicyResponse(
            enabled=policy.enabled,
            policy_type=policy.policy_type,
            finite_horizon_days=policy.finite_horizon_days,
            revision=policy.revision,
        ),
        capabilities=VaultCapabilitiesResponse(
            indefinite_retention=production_indefinite_gate_open(),
            deletion_scopes=["message", "conversation", "participant", "all"],
            unlink_archive_treatments=["preserve", "delete"],
        ),
        deletion_operation=(
            None if outstanding is None else _operation_response(outstanding)
        ),
    )


def _target(request: VaultCommandRequest) -> str:
    if request.target_id is None or not request.target_id.strip():
        raise HTTPException(status_code=422, detail="target_id is required for this action")
    return request.target_id.strip()


def _reject_unexpected(
    request: VaultCommandRequest,
    *,
    allow_horizon: bool = False,
    allow_target: bool = False,
    allow_unlink_treatment: bool = False,
) -> None:
    if not allow_horizon and request.finite_horizon_days is not None:
        raise HTTPException(status_code=422, detail="finite_horizon_days is not valid for this action")
    if not allow_target and request.target_id is not None:
        raise HTTPException(status_code=422, detail="target_id is not valid for this action")
    if not allow_unlink_treatment and request.unlink_archive_treatment is not None:
        raise HTTPException(
            status_code=422,
            detail="unlink_archive_treatment is not valid for this action",
        )


async def _finish_deletion(
    account_id: str,
    operation_id: str,
    operations: ManagedDeletionOperations,
) -> ManagedDeletionOperation:
    try:
        await transport_manager.project_committed_state(account_id)
    except Exception:
        return operations.mark_incomplete(
            account_id,
            operation_id,
            "dependent_state_cleanup_failed",
        )
    operation = operations.get(account_id, operation_id)
    if operation is None:
        raise RuntimeError("managed deletion operation disappeared")
    return operation


@router.get("", response_model=VaultStatusResponse)
def get_creator_vault_status(
    response: Response,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
) -> VaultStatusResponse:
    require_creator(policy)
    assert policy.identity is not None
    response.headers["Cache-Control"] = "no-store"
    return _status(policy.identity.creator_account_id)


@router.post("/commands", response_model=VaultCommandResponse)
async def apply_creator_vault_command(
    request: Request,
    command: VaultCommandRequest,
    response: Response,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> VaultCommandResponse:
    require_creator(policy)
    verify_same_origin(request)
    verify_csrf_token(policy, csrf)
    assert policy.identity is not None
    account_id = policy.identity.creator_account_id
    retention = _retention()
    operations = _deletions()
    deletion_revision: int | None = None
    deletion_operation: ManagedDeletionOperation | None = None
    unlink_treatment: UnlinkArchiveTreatment | None = None
    action_ref = f"creator-control:{command.action}:{uuid4()}"

    try:
        if command.action == "enable_finite":
            _reject_unexpected(command, allow_horizon=True)
            if command.finite_horizon_days is None:
                raise HTTPException(
                    status_code=422,
                    detail="finite_horizon_days is required for finite retention",
                )
            retention.set_policy(
                account_id,
                "finite",
                finite_horizon_days=command.finite_horizon_days,
                creator_action_ref=action_ref,
            )
        elif command.action == "enable_indefinite":
            _reject_unexpected(command)
            if not production_indefinite_gate_open():
                raise HTTPException(
                    status_code=409,
                    detail="indefinite_retention_unavailable",
                )
            retention.set_policy(
                account_id,
                "indefinite_until_delete",
                creator_action_ref=action_ref,
            )
        elif command.action == "disable":
            _reject_unexpected(command)
            retention.set_policy(
                account_id,
                "disabled",
                creator_action_ref=action_ref,
            )
        elif command.action == "delete_message":
            _reject_unexpected(command, allow_target=True)
            deletion_revision = retention.delete_message(account_id, _target(command))
        elif command.action == "delete_conversation":
            _reject_unexpected(command, allow_target=True)
            deletion_revision = retention.delete_conversation(account_id, _target(command))
        elif command.action == "delete_participant":
            _reject_unexpected(command, allow_target=True)
            deletion_revision = retention.delete_participant(account_id, _target(command))
        elif command.action == "delete_all":
            _reject_unexpected(command)
            operation_id = str(uuid4())
            deletion_revision = retention.delete_all(
                account_id, provenance=f"creator_delete:{operation_id}"
            )
            deletion_operation = await _finish_deletion(
                account_id, operation_id, operations
            )
        else:
            _reject_unexpected(command, allow_unlink_treatment=True)
            if command.unlink_archive_treatment is None:
                raise HTTPException(
                    status_code=422,
                    detail="unlink_archive_treatment is required for unlink",
                )
            unlink_treatment = command.unlink_archive_treatment
            if unlink_treatment == "preserve":
                retention.unlink(account_id, preserve_archive=True)
            else:
                operation_id = str(uuid4())
                deletion_revision = retention.delete_all(
                    account_id, provenance=f"unlink_delete:{operation_id}"
                )
                deletion_operation = await _finish_deletion(
                    account_id, operation_id, operations
                )
    except RetentionPolicyError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    response.headers["Cache-Control"] = "no-store"
    return VaultCommandResponse(
        action=command.action,
        status=_status(account_id),
        deletion_revision=deletion_revision,
        deletion_operation=(
            None if deletion_operation is None else _operation_response(deletion_operation)
        ),
        unlink_archive_treatment=unlink_treatment,
    )


@router.post(
    "/deletions/{operation_id}/retry",
    response_model=VaultDeletionOperationResponse,
)
async def retry_creator_vault_deletion(
    operation_id: str,
    request: Request,
    response: Response,
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
    csrf: str | None = Header(None, alias="X-CSRF-Token"),
) -> VaultDeletionOperationResponse:
    require_creator(policy)
    verify_same_origin(request)
    verify_csrf_token(policy, csrf)
    assert policy.identity is not None
    account_id = policy.identity.creator_account_id
    operations = _deletions()
    operation = operations.get(account_id, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="deletion_operation_not_found")
    if operation.status != "complete":
        operation = await _finish_deletion(account_id, operation_id, operations)
    response.headers["Cache-Control"] = "no-store"
    return _operation_response(operation)


@router.get("/export")
def export_creator_vault(
    policy: RuntimePolicy = Depends(get_authenticated_runtime_policy),
) -> JSONResponse:
    require_creator(policy)
    assert policy.identity is not None
    export = CreatorVaultExporter(transport_manager.canonical_database).build(
        policy.identity.creator_account_id
    )
    return JSONResponse(
        content=export.document(),
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="creator-vault-export.json"',
        },
    )
