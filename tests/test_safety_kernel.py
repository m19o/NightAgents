from datetime import UTC, datetime, timedelta

from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.mythic import Callback, CallbackInventory, RouteVerification
from nightagents.safety.kernel import SafetyKernel, Transition


def inventory(status: ObservationStatus = ObservationStatus.SUCCESS) -> CallbackInventory:
    callback = Callback(
        callback_id="cb-1",
        operation_id="eng-1",
        payload_type="poseidon",
        c2_profile="http",
        route_id="old",
        last_seen=datetime.now(UTC),
        expected_checkin_seconds=60,
        active=True,
    )
    return CallbackInventory(
        status=status,
        callbacks=[callback] if status is ObservationStatus.SUCCESS else [],
        observed_at=datetime.now(UTC),
    )


def test_unknown_inventory_blocks_drain() -> None:
    decision = SafetyKernel().authorize_transition(
        Transition.DRAIN_OLD_ROUTE,
        inventory(ObservationStatus.UNAVAILABLE),
        None,
        datetime.now(UTC) + timedelta(minutes=1),
    )
    assert not decision.allowed


def test_confirmed_replacement_allows_drain() -> None:
    verification = RouteVerification(
        status=VerificationStatus.CONFIRMED,
        route_id="new",
        observed_callback_ids=["cb-1"],
        reason="observed",
    )
    decision = SafetyKernel().authorize_transition(
        Transition.DRAIN_OLD_ROUTE,
        inventory(),
        verification,
        datetime.now(UTC) + timedelta(minutes=1),
    )
    assert decision.allowed
