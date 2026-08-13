"""The carried cost refuses interpretation at runtime, whatever path it takes.

The paths exercised here are the ones a single-expression syntactic guard cannot
see: one local, two locals, a container bound to a local, and a function
parameter.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from app.security.admission_confirmation import (
    ConfirmationSubject,
    displayed_credit_cost,
    issue_admission_confirmation,
    open_admission_confirmation,
)
from app.security.permit_reserve_port import ReserveRequest, reserve_admission
from app.security.runtime_policy import AuthContext, AuthorizationEpoch, RuntimePolicy


CONFIRMED_AT = 1_760_000_000
CARRIED_COST = 250_000


def _identity() -> AuthContext:
    return AuthContext(
        principal_id="principal-fixture-001",
        creator_account_id="account-fixture-001",
        role="creator",
        platform_creator_id="platform-creator-fixture-001",
        session_id="session-fixture-001",
        session_expires_at=CONFIRMED_AT + 3_600,
    )


def _policy() -> RuntimePolicy:
    return RuntimePolicy(identity=_identity(), authorization_epoch=AuthorizationEpoch(7))


def _subject(**overrides: object) -> ConfirmationSubject:
    fields: dict[str, object] = {
        "organization_id": "organization-fixture-001",
        "installation_id": "installation-fixture-001",
        "request_idempotency_key": "9f1c5a2e-0000-4000-8000-000000000001",
        "permit_id": "01920000-0000-7000-8000-000000000001",
        "job_id": "job-fixture-001",
        "promised_generation_id": "generation-fixture-001",
        "capability": "analysis-run",
        "parameter_digest": "sha256:" + "a" * 64,
        "input_revision": 41,
        "input_selection_digest": "sha256:" + "b" * 64,
        "credit_cost": CARRIED_COST,
    }
    fields.update(overrides)
    return ConfirmationSubject(**fields)  # type: ignore[arg-type]


def _reserve_request(**overrides: object) -> ReserveRequest:
    fields: dict[str, object] = {
        "permit_id": "01920000-0000-7000-8000-000000000001",
        "job_id": "job-fixture-001",
        "capability": "analysis-run",
        "parameter_digest": "sha256:" + "a" * 64,
        "input_revision": 41,
        "input_selection_digest": "sha256:" + "b" * 64,
        "request_idempotency_key": "9f1c5a2e-0000-4000-8000-000000000001",
        "permit_credit_cost": CARRIED_COST,
        "installation_matches": True,
        "permit_state": "available",
        "reserved_job_id": None,
        "durable_output_committed": False,
    }
    fields.update(overrides)
    return ReserveRequest(**fields)  # type: ignore[arg-type]


def _carried_type() -> type:
    return type(_subject().credit_cost)


def _attribute(subject: ConfirmationSubject) -> Any:
    return subject.credit_cost


def _one_local(subject: ConfirmationSubject) -> Any:
    cost = subject.credit_cost
    return cost


def _two_locals(subject: ConfirmationSubject) -> Any:
    cost = subject.credit_cost
    carried = cost
    return carried


def _container_local(subject: ConfirmationSubject) -> Any:
    row = {"cost": subject.credit_cost}
    return row["cost"]


def _function_parameter(subject: ConfirmationSubject) -> Any:
    def relay(value: Any) -> Any:
        return value

    return relay(subject.credit_cost)


_DELIVERIES: tuple[tuple[str, Callable[[ConfirmationSubject], Any]], ...] = (
    ("attribute", _attribute),
    ("one_local", _one_local),
    ("two_locals", _two_locals),
    ("container_local", _container_local),
    ("function_parameter", _function_parameter),
)

_INTERPRETATIONS: tuple[tuple[str, Callable[[Any], Any]], ...] = (
    ("add", lambda value: value + 1),
    ("radd", lambda value: 1 + value),
    ("sub", lambda value: value - 1),
    ("rsub", lambda value: 1 - value),
    ("mul", lambda value: value * 2),
    ("rmul", lambda value: 2 * value),
    ("neg", lambda value: -value),
    ("lt", lambda value: value < 1),
    ("le", lambda value: value <= 1),
    ("gt", lambda value: value > 1),
    ("ge", lambda value: value >= 1),
    ("int", int),
    ("float", float),
)

_BASELINE_DUNDERS = (
    "__abs__",
    "__add__",
    "__float__",
    "__floordiv__",
    "__ge__",
    "__gt__",
    "__index__",
    "__int__",
    "__le__",
    "__lt__",
    "__mul__",
    "__neg__",
    "__pos__",
    "__radd__",
    "__rmul__",
    "__round__",
    "__rsub__",
    "__sub__",
    "__truediv__",
    "__trunc__",
)


@pytest.mark.parametrize(
    "deliver", [pytest.param(hop, id=name) for name, hop in _DELIVERIES]
)
@pytest.mark.parametrize(
    "interpret", [pytest.param(op, id=name) for name, op in _INTERPRETATIONS]
)
def test_carried_cost_refuses_interpretation_after_any_hop(
    deliver: Callable[[ConfirmationSubject], Any],
    interpret: Callable[[Any], Any],
) -> None:
    carried = deliver(_subject())

    with pytest.raises(TypeError):
        interpret(carried)


def test_carried_cost_type_adds_no_arithmetic_ordering_or_coercion() -> None:
    carried = _carried_type()

    added = [
        name
        for name in _BASELINE_DUNDERS
        if getattr(carried, name, None) is not getattr(object, name, None)
    ]

    assert (carried.__name__, added) == ("CarriedCost", [])


def test_carried_cost_of_the_same_wire_value_is_equal() -> None:
    assert _subject().credit_cost == _subject().credit_cost


@pytest.mark.parametrize("other", [1, True, 250_000.0, "250000", None])
def test_carried_cost_of_another_wire_value_or_type_is_not_equal(other: object) -> None:
    assert _subject().credit_cost != _subject(credit_cost=other).credit_cost


def test_carried_cost_is_not_equal_to_the_bare_wire_value() -> None:
    assert _subject().credit_cost != CARRIED_COST


@pytest.mark.parametrize(
    ("wire_value", "text"),
    [
        (CARRIED_COST, "250000"),
        (0, "0"),
        (250_000.0, "250000.0"),
        ("250000", "250000"),
        (True, "true"),
        (None, "null"),
    ],
)
def test_carried_cost_displays_the_wire_value_verbatim(
    wire_value: object, text: str
) -> None:
    carried = _subject(credit_cost=wire_value).credit_cost

    assert (str(carried), f"{carried}") == (text, text)


def test_carried_cost_refuses_a_format_specification() -> None:
    carried = _subject().credit_cost

    with pytest.raises(TypeError):
        format(carried, ",")


def test_carried_cost_is_recorded_verbatim() -> None:
    subject = _subject()
    confirmation, _ = issue_admission_confirmation(
        _policy(), subject, issued_at=CONFIRMED_AT
    )

    assert confirmation.durable_record()["credit_cost"] is CARRIED_COST
    assert displayed_credit_cost(subject) is subject.credit_cost


def test_carried_cost_survives_the_token_round_trip_as_a_carried_value() -> None:
    policy = _policy()
    _, token = issue_admission_confirmation(policy, _subject(), issued_at=CONFIRMED_AT)

    reopened = open_admission_confirmation(policy, token, now=CONFIRMED_AT)

    assert type(reopened.subject.credit_cost) is _carried_type()
    assert reopened.subject.credit_cost == _subject().credit_cost
    with pytest.raises(TypeError):
        reopened.subject.credit_cost + 1


def test_reserve_request_carries_the_presented_cost_as_a_carried_value() -> None:
    request = _reserve_request()

    assert type(request.permit_credit_cost) is _carried_type()
    with pytest.raises(TypeError):
        request.permit_credit_cost > 0


def test_confirmed_cost_still_admits_and_a_repriced_cost_is_still_refused() -> None:
    policy = _policy()
    _, token = issue_admission_confirmation(policy, _subject(), issued_at=CONFIRMED_AT)

    admitted = reserve_admission(
        policy, token, request=_reserve_request(), now=CONFIRMED_AT + 30
    )
    refused = reserve_admission(
        policy,
        token,
        request=_reserve_request(permit_credit_cost=250_001),
        now=CONFIRMED_AT + 30,
    )

    assert (admitted.reserved, admitted.result) == (True, "reserved")
    assert (refused.reserved, refused.result) == (False, "confirmation_cost_mismatch")
