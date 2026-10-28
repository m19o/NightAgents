"""Lab-only infrastructure provider: moves real demo traffic.

Repoints the victim container's resolver so the beacon's callback hostname
resolves to the standby redirector's IP (execute), restores it (rollback), and
verifies continuity from redirector access logs in Elastic - independent edge
correlation, the evidence the architecture requires for a CONFIRMED verdict.

Lab scope only: it drives the local docker daemon over the socket and the demo
Elastic data stream. Production deployments implement `InfrastructurePort`
against a real DNS/edge provider instead.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from nightagents.contracts.common import ObservationStatus, VerificationStatus
from nightagents.contracts.infrastructure import InfrastructurePlan, InfrastructureResult, RouteHealth
from nightagents.contracts.mythic import RouteVerification

MARKER = "nightagents-route-switch"


class DockerError(RuntimeError):
    pass


class LabInfrastructureProvider:
    def __init__(
        self,
        victim_container: str,
        hostname: str,
        redirector_map: dict[str, str],
        elastic_url: str,
        data_stream: str = "logs-redirector-demo",
        docker_socket: str = "/var/run/docker.sock",
        docker_client: httpx.AsyncClient | None = None,
        elastic_client: httpx.AsyncClient | None = None,
    ):
        self.victim_container = victim_container
        self.hostname = hostname
        self.redirector_map = redirector_map
        self.elastic_url = elastic_url.rstrip("/")
        self.data_stream = data_stream
        self._docker = docker_client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=docker_socket),
            base_url="http://docker",
            timeout=30,
        )
        self._elastic = elastic_client or httpx.AsyncClient(timeout=15)
        self._switches: dict[str, dict] = {}

    # ------------------------------------------------------------------ docker

    async def _inspect(self, container: str) -> dict:
        response = await self._docker.get(f"/containers/{container}/json")
        if response.status_code != 200:
            raise DockerError(f"container {container} not found")
        return response.json()

    async def _container_ip(self, container: str) -> str:
        data = await self._inspect(container)
        networks = (data.get("NetworkSettings") or {}).get("Networks") or {}
        for network in networks.values():
            if network.get("IPAddress"):
                return network["IPAddress"]
        raise DockerError(f"container {container} has no IP")

    async def _exec(self, container: str, command: list[str]) -> str:
        create = await self._docker.post(
            f"/containers/{container}/exec",
            json={"AttachStdout": True, "AttachStderr": True, "Cmd": command},
        )
        if create.status_code != 201:
            raise DockerError(f"exec create failed in {container}: {create.text[:200]}")
        exec_id = create.json()["Id"]
        start = await self._docker.post(f"/exec/{exec_id}/start", json={"Detach": False, "Tty": False})
        if start.status_code != 200:
            raise DockerError(f"exec start failed in {container}: {start.text[:200]}")
        return start.text

    # ------------------------------------------------------------------ port

    async def route_health(self, engagement_id: str, redirector_id: str) -> RouteHealth:
        container = self.redirector_map.get(redirector_id)
        if not container:
            return RouteHealth(
                route_id=redirector_id,
                status=ObservationStatus.UNAVAILABLE,
                healthy=False,
                observed_at=datetime.now(UTC),
                checks={},
                error=f"redirector {redirector_id} not in lab map",
            )
        try:
            data = await self._inspect(container)
            running = bool((data.get("State") or {}).get("Running"))
            return RouteHealth(
                route_id=redirector_id,
                status=ObservationStatus.SUCCESS,
                healthy=running,
                observed_at=datetime.now(UTC),
                checks={"container": container, "running": running},
            )
        except DockerError as exc:
            return RouteHealth(
                route_id=redirector_id,
                status=ObservationStatus.UNAVAILABLE,
                healthy=False,
                observed_at=datetime.now(UTC),
                checks={"container": container},
                error=str(exc),
            )

    async def plan_route_switch(
        self,
        engagement_id: str,
        logical_route_id: str,
        old_redirector_id: str,
        new_redirector_id: str,
    ) -> InfrastructurePlan:
        if new_redirector_id not in self.redirector_map:
            raise ValueError(f"redirector {new_redirector_id} not in lab map")
        return InfrastructurePlan(
            plan_id=str(uuid4()),
            engagement_id=engagement_id,
            logical_route_id=logical_route_id,
            old_redirector_id=old_redirector_id,
            new_redirector_id=new_redirector_id,
            preconditions=["victim resolver reachable", "standby container running"],
            steps=[f"repoint {self.hostname} to standby IP in the victim resolver"],
            rollback_steps=[f"remove {MARKER} entries from the victim resolver"],
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )

    async def execute_route_switch(self, plan: InfrastructurePlan, idempotency_key: str) -> InfrastructureResult:
        container = self.redirector_map[plan.new_redirector_id]
        try:
            standby_ip = await self._container_ip(container)
            await self._exec(
                self.victim_container,
                [
                    "sh",
                    "-c",
                    f'grep -q "{idempotency_key}" /etc/hosts || '
                    f'echo "{standby_ip} {self.hostname} # {MARKER} {idempotency_key}" >> /etc/hosts',
                ],
            )
            switched_at = datetime.now(UTC)
            self._switches[plan.new_redirector_id] = {
                "container": container,
                "ip": standby_ip,
                "switched_at": switched_at.isoformat(),
            }
            return InfrastructureResult(
                success=True,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                previous_state={"resolved_via": "docker-embedded-dns"},
                current_state={
                    "hostname": self.hostname,
                    "resolves_to": standby_ip,
                    "container": container,
                    "switched_at": switched_at.isoformat(),
                },
                message=f"{self.hostname} now resolves to {standby_ip} ({container}) on the victim",
            )
        except DockerError as exc:
            return InfrastructureResult(
                success=False,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                message=str(exc),
            )

    async def rollback_route_switch(self, plan: InfrastructurePlan, idempotency_key: str) -> InfrastructureResult:
        try:
            await self._exec(
                self.victim_container,
                ["sh", "-c", f"sed -i '/# {MARKER}/d' /etc/hosts"],
            )
            self._switches.pop(plan.new_redirector_id, None)
            return InfrastructureResult(
                success=True,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                current_state={"resolver": "restored"},
                message="victim resolver restored to docker DNS",
            )
        except DockerError as exc:
            return InfrastructureResult(
                success=False,
                plan_id=plan.plan_id,
                idempotency_key=idempotency_key,
                message=str(exc),
            )

    # ------------------------------------------------------------- verification

    async def verify_callback_route(
        self, engagement_id: str, route_id: str, callback_ids: list[str]
    ) -> RouteVerification:
        """Independent edge correlation: beacons arriving at the standby
        redirector in the shared access logs after the switch. Observes for a
        bounded window - beacons arrive on an interval, so instant checks
        always find nothing."""
        switch = self._switches.get(route_id)
        if switch is None:
            return RouteVerification(
                status=VerificationStatus.UNKNOWN,
                route_id=route_id,
                reason="no lab route switch recorded for this route",
            )
        deadline = datetime.now(UTC) + timedelta(seconds=45)
        total = 0
        error: str | None = None
        while datetime.now(UTC) < deadline:
            try:
                response = await self._elastic.post(
                    f"{self.elastic_url}/{self.data_stream}/_search",
                    json={
                        "size": 5,
                        "query": {
                            "bool": {
                                "filter": [
                                    {"term": {"host.name": switch["container"]}},
                                    {"range": {"@timestamp": {"gte": switch["switched_at"]}}},
                                ]
                            }
                        },
                    },
                )
                response.raise_for_status()
                total = response.json().get("hits", {}).get("total", {}).get("value", 0)
                error = None
            except Exception as exc:
                error = str(exc)
            if total >= 2:
                break
            await asyncio.sleep(5)
        if error is not None and total == 0:
            return RouteVerification(
                status=VerificationStatus.UNKNOWN,
                route_id=route_id,
                reason=f"edge telemetry unavailable: {error}",
            )
        if total >= 2:
            return RouteVerification(
                status=VerificationStatus.CONFIRMED,
                route_id=route_id,
                observed_callback_ids=list(callback_ids),
                missing_callback_ids=[],
                reason=f"{total} beacon events observed on {switch['container']} after the switch",
            )
        return RouteVerification(
            status=VerificationStatus.UNKNOWN,
            route_id=route_id,
            missing_callback_ids=list(callback_ids),
            reason=f"only {total} beacon events on {switch['container']} after the switch",
        )
