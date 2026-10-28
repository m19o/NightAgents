"""Cloudflare infrastructure adapter.

Real third-party redirector control behind `InfrastructurePort`: health from
Cloudflare Analytics + zone status, route switches as reversible DNS repoints,
all sense/actuate calls out-of-band to the Cloudflare control plane. Nothing is
placed on, and nothing is pulled from, any origin host.

Every mutation still flows through the Safety Kernel's execution grants at the
infrastructure MCP layer - this adapter only implements the port contract.
"""

from nightagents.adapters.cloudflare.client import CloudflareClient, CloudflareError
from nightagents.adapters.cloudflare.provider import CloudflareInfrastructure
from nightagents.adapters.cloudflare.sandbox import sandbox_transport, set_scenario

__all__ = [
    "CloudflareClient",
    "CloudflareError",
    "CloudflareInfrastructure",
    "sandbox_transport",
    "set_scenario",
]
