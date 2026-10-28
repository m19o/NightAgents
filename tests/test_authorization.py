from datetime import UTC, datetime, timedelta

import pytest

from nightagents.adapters.in_memory import InMemoryMythic
from nightagents.contracts.mythic import Callback, ExecutionGrant
from nightagents.mcp_servers.mythic.service import MythicMCPService
from nightagents.security.authorization import PRINCIPALS, AuthorizationError
from nightagents.security.signing import sign_hash


def callback(route_id: str = "route-a") -> Callback:
    return Callback(
        callback_id="cb-1",
        operation_id="eng-1",
        payload_type="poseidon",
        c2_profile="http",
        route_id=route_id,
        last_seen=datetime.now(UTC),
        expected_checkin_seconds=60,
        active=True,
    )


async def test_nightwatcher_can_read_but_not_plan() -> None:
    service = MythicMCPService(InMemoryMythic([callback()]))
    inventory = await service.list_callbacks(PRINCIPALS["nightwatcher"], "eng-1")
    assert len(inventory.callbacks) == 1
    with pytest.raises(AuthorizationError):
        await service.plan_callback_migration(
            PRINCIPALS["nightwatcher"], "eng-1", "route-a", "route-b", ["cb-1"]
        )


async def test_nightrouter_plans_and_safety_kernel_executes_exact_plan() -> None:
    secret = "grant-secret"
    service = MythicMCPService(InMemoryMythic([callback()]), grant_secret=secret)
    plan = await service.plan_callback_migration(
        PRINCIPALS["nightrouter"], "eng-1", "route-a", "route-b", ["cb-1"]
    )
    digest = "approved-proposal-hash"
    grant = ExecutionGrant(
        grant_id="grant-1",
        proposal_hash=digest,
        engagement_id="eng-1",
        allowed_tool="mythic.apply_route_plan",
        allowed_plan_id=plan.plan_id,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        signature=sign_hash(secret, digest),
    )
    result = await service.apply_route_plan(PRINCIPALS["safety-kernel"], plan, grant, "idem-1")
    duplicate = await service.apply_route_plan(PRINCIPALS["safety-kernel"], plan, grant, "idem-1")
    assert result.success
    assert duplicate == result

