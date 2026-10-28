from dataclasses import dataclass

from nightagents.contracts.common import Capability


class AuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class Principal:
    principal_id: str
    capabilities: frozenset[Capability]


class CapabilityPolicy:
    def require(self, principal: Principal, capability: Capability) -> None:
        if capability not in principal.capabilities:
            raise AuthorizationError(f"{principal.principal_id} lacks {capability.value} capability")


PRINCIPALS = {
    "nightwatcher": Principal("nightwatcher", frozenset({Capability.READ, Capability.VERIFY})),
    "nightrouter": Principal(
        "nightrouter", frozenset({Capability.READ, Capability.VERIFY, Capability.PLAN})
    ),
    "safety-kernel": Principal("safety-kernel", frozenset({Capability.EXECUTE})),
    "research-agent": Principal(
        "research-agent", frozenset({Capability.READ, Capability.VERIFY, Capability.PLAN})
    ),
}
