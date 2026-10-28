import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.orm import Session

from nightagents.db.models import ToolCallRecord

if TYPE_CHECKING:
    from nightagents.skills.registry import SkillManifest


class MCPToolError(RuntimeError):
    pass


def _read_timeout(seconds: float) -> object:
    """Build the read-timeout value expected by the installed MCP SDK.

    Released SDK versions expect a ``datetime.timedelta``; the snapshot API
    expects raw seconds as a float. Detected once from the call annotation.
    """
    import inspect

    annotation = inspect.signature(ClientSession.call_tool).parameters["read_timeout_seconds"].annotation
    if "timedelta" in str(annotation):
        return timedelta(seconds=seconds)
    return seconds


class MCPGateway(Protocol):
    async def call_tool(self, server: str, tool: str, arguments: dict) -> dict | list: ...


@dataclass
class _TokenBucket:
    rate_per_second: float
    _tokens: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self):
        self._tokens = self.rate_per_second
        self._last = monotonic()

    async def acquire(self) -> None:
        now = monotonic()
        elapsed = now - self._last
        self._tokens = min(self.rate_per_second, self._tokens + elapsed * self.rate_per_second)
        self._last = now
        if self._tokens < 1:
            wait = (1 - self._tokens) / self.rate_per_second if self.rate_per_second else 0
            await asyncio.sleep(wait)
            self._tokens = 0
        else:
            self._tokens -= 1


@dataclass
class _PooledClient:
    client: httpx.AsyncClient
    created_at: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RemoteMCPGateway:
    def __init__(
        self,
        endpoints: dict[str, dict],
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
        maximum_concurrency: int = 8,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: float = 30,
        client_ttl_seconds: float = 60,
        rate_per_second: float = 10,
    ):
        self.endpoints = endpoints
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._tool_cache: dict[str, dict[str, dict]] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self.maximum_concurrency = maximum_concurrency
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self.client_ttl_seconds = client_ttl_seconds
        self.rate_per_second = rate_per_second
        self._health: dict[str, dict] = {}
        self._clients: dict[str, _PooledClient] = {}
        self._rate_limiters: dict[str, _TokenBucket] = {}
        self._pool_lock = asyncio.Lock()
        self._active_calls: dict[str, int] = {}

    @classmethod
    def from_json(cls, value: str) -> "RemoteMCPGateway":
        return cls(json.loads(value or "{}"))

    async def call_tool(self, server: str, tool: str, arguments: dict) -> dict | list:
        health = self._health.setdefault(
            server,
            {"consecutive_failures": 0, "circuit_open_until": 0.0, "successful_calls": 0, "failed_calls": 0},
        )
        if health["circuit_open_until"] > monotonic():
            raise MCPToolError(f"MCP circuit is open: {server}")
        semaphore = self._semaphores.setdefault(server, asyncio.Semaphore(self.maximum_concurrency))
        async with semaphore:
            self._active_calls[server] = self._active_calls.get(server, 0) + 1
            try:
                result = await self._call_tool_with_reconnect(server, tool, arguments)
            except Exception:
                health["consecutive_failures"] += 1
                health["failed_calls"] += 1
                if health["consecutive_failures"] >= self.circuit_failure_threshold:
                    health["circuit_open_until"] = monotonic() + self.circuit_cooldown_seconds
                raise
            finally:
                self._active_calls[server] = max(0, self._active_calls.get(server, 1) - 1)
            health["consecutive_failures"] = 0
            health["circuit_open_until"] = 0.0
            health["successful_calls"] += 1
            return result

    async def ping(self, server: str) -> bool:
        """Lightweight health check: list tools and measure round-trip."""
        try:
            await self.list_tools(server, refresh=True)
            return True
        except Exception:
            return False

    async def check_health(self) -> dict[str, dict]:
        """Ping all configured servers and return per-server health."""
        results: dict[str, dict] = {}
        for server in self.endpoints:
            healthy = await self.ping(server)
            health = self._health.setdefault(
                server,
                {"consecutive_failures": 0, "circuit_open_until": 0.0, "successful_calls": 0, "failed_calls": 0},
            )
            if not healthy:
                health["failed_calls"] += 1
                health["consecutive_failures"] += 1
            else:
                health["consecutive_failures"] = 0
            results[server] = {**health, "healthy": healthy}
        return results

    def backpressure_metrics(self) -> dict[str, dict]:
        """Return per-server active call counts and queue depth."""
        return {
            server: {
                "active_calls": self._active_calls.get(server, 0),
                "max_concurrency": self.maximum_concurrency,
                "rate_per_second": self.rate_per_second,
            }
            for server in self.endpoints
        }

    async def _call_tool_with_reconnect(self, server: str, tool: str, arguments: dict) -> dict | list:
        if self.rate_per_second > 0:
            limiter = self._rate_limiters.setdefault(server, _TokenBucket(self.rate_per_second))
            await limiter.acquire()
        last_error: Exception | None = None
        for _attempt in range(2):
            pooled = await self._get_or_create_client(server)
            try:
                return await self._call_tool_once(server, tool, arguments, pooled.client)
            except Exception as exc:
                last_error = exc
                async with self._pool_lock:
                    if self._clients.get(server) is pooled:
                        await self._close_client(pooled.client)
                        self._clients.pop(server, None)
        raise last_error or MCPToolError(f"MCP call failed: {server}.{tool}")

    async def _call_tool_once(
        self, server: str, tool: str, arguments: dict, http_client: httpx.AsyncClient
    ) -> dict | list:
        endpoint = self._endpoint(server)
        async with streamable_http_client(endpoint["url"], http_client=http_client) as streams:
            read_stream, write_stream = streams[0], streams[1]
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool, arguments, read_timeout_seconds=_read_timeout(self.timeout_seconds)
                )
        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            message = " ".join(getattr(block, "text", str(block)) for block in result.content)
            raise MCPToolError(f"{server}.{tool} failed: {message}")
        structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))
        if structured is not None:
            return structured
        text = "\n".join(getattr(block, "text", "") for block in result.content).strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    async def _get_or_create_client(self, server: str) -> _PooledClient:
        async with self._pool_lock:
            pooled = self._clients.get(server)
            now = monotonic()
            if pooled is None or (now - pooled.created_at) > self.client_ttl_seconds:
                if pooled is not None:
                    await self._close_client(pooled.client)
                endpoint = self._endpoint(server)
                headers = self._headers(endpoint)
                client = httpx.AsyncClient(
                    headers=headers, timeout=self.timeout_seconds, transport=self.transport
                )
                pooled = _PooledClient(client=client, created_at=now)
                self._clients[server] = pooled
            return pooled

    async def _close_client(self, client: httpx.AsyncClient) -> None:
        try:
            await client.aclose()
        except Exception:
            pass

    async def close(self) -> None:
        async with self._pool_lock:
            for pooled in list(self._clients.values()):
                await self._close_client(pooled.client)
            self._clients.clear()

    async def discover_all(self) -> dict[str, dict[str, dict]]:
        discovered: dict[str, dict[str, dict]] = {}
        for server in self.endpoints:
            health = self._health.setdefault(
                server,
                {"consecutive_failures": 0, "circuit_open_until": 0.0, "successful_calls": 0, "failed_calls": 0},
            )
            try:
                discovered[server] = await self.list_tools(server)
                health["successful_calls"] += 1
            except Exception as exc:
                health["failed_calls"] += 1
                discovered[server] = {"_error": str(exc)}
        return discovered

    async def validate_required_tools(self, required: list[str]) -> dict:
        missing: list[str] = []
        discovered = await self.discover_all()
        for declaration in required:
            if "." not in declaration:
                missing.append(declaration)
                continue
            server, tool = declaration.split(".", 1)
            server_tools = discovered.get(server, {})
            if "_error" in server_tools:
                missing.append(f"{declaration} ({server_tools['_error']})")
                continue
            if tool not in server_tools:
                missing.append(declaration)
        return {
            "valid": not missing,
            "missing": missing,
            "servers_checked": list(discovered.keys()),
        }

    def health_snapshot(self) -> dict[str, dict]:
        now = monotonic()
        return {
            server: {
                **state,
                "circuit_open": state["circuit_open_until"] > now,
                "circuit_open_remaining_seconds": max(round(state["circuit_open_until"] - now, 3), 0),
                "client_active": server in self._clients,
            }
            for server, state in self._health.items()
        }

    async def list_tools(self, server: str, refresh: bool = False) -> dict[str, dict]:
        if server in self._tool_cache and not refresh:
            return self._tool_cache[server]
        endpoint = self._endpoint(server)
        headers = self._headers(endpoint)
        async with httpx.AsyncClient(
            headers=headers, timeout=self.timeout_seconds, transport=self.transport
        ) as http_client:
            async with streamable_http_client(endpoint["url"], http_client=http_client) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.list_tools()
        tools = {}
        for tool in result.tools:
            value = tool.model_dump(by_alias=True)
            tools[value["name"]] = value.get("inputSchema", value.get("input_schema", {}))
        self._tool_cache[server] = tools
        return tools

    async def validate_manifest(self, manifest: "SkillManifest") -> None:
        discovered: dict[str, dict[str, dict]] = {}
        for declaration in manifest.tools:
            server, tool = declaration.split(".", 1)
            if server not in discovered:
                discovered[server] = await self.list_tools(server)
            if tool not in discovered[server]:
                raise MCPToolError(f"MCP contract mismatch: {declaration} is not advertised")
        for phase in manifest.phases:
            if not phase.tool:
                continue
            server, tool = phase.tool.split(".", 1)
            schema = discovered[server][tool]
            properties = set(schema.get("properties", {}))
            unknown = set(phase.arguments) - properties
            if unknown and schema.get("additionalProperties") is False:
                names = ", ".join(sorted(unknown))
                raise MCPToolError(f"MCP contract mismatch: {phase.tool} rejects arguments: {names}")

    def _endpoint(self, server: str) -> dict:
        endpoint = self.endpoints.get(server)
        if not endpoint:
            raise MCPToolError(f"MCP endpoint is not configured: {server}")
        return endpoint

    @staticmethod
    def _headers(endpoint: dict) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream"}
        if endpoint.get("token"):
            headers["Authorization"] = f"Bearer {endpoint['token']}"
        return headers


class RecordedMCPGateway:
    def __init__(self, db: Session, run_id: str, delegate: MCPGateway):
        self.db = db
        self.run_id = run_id
        self.delegate = delegate

    async def call_tool(self, server: str, tool: str, arguments: dict) -> dict | list:
        started = monotonic()
        record = ToolCallRecord(
            run_id=self.run_id,
            server=server,
            tool=tool,
            request_json=arguments,
            status="running",
            created_at=datetime.now(UTC),
        )
        self.db.add(record)
        self.db.commit()
        try:
            result = await self.delegate.call_tool(server, tool, arguments)
            record.response_json = result if isinstance(result, dict) else {"items": result}
            record.status = "completed"
            return result
        except Exception as exc:
            record.response_json = {"error": str(exc)}
            record.status = "failed"
            raise
        finally:
            record.duration_ms = int((monotonic() - started) * 1000)
            self.db.commit()
