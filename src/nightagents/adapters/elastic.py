from datetime import UTC, datetime, timedelta

import httpx

from nightagents.mcp_servers.elastic.catalog import QUERY_CATALOG


class ElasticAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        verify_tls: bool = True,
        timeout_seconds: float = 10,
        source_max_age_seconds: int = 900,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"ApiKey {api_key}"
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            verify=verify_tls,
            timeout=timeout_seconds,
            transport=transport,
        )
        self.source_max_age_seconds = source_max_age_seconds

    async def close(self) -> None:
        await self.client.aclose()

    async def source_health(self, source_ids: list[str]) -> dict:
        try:
            cluster = await self.client.get("/_cluster/health")
            cluster.raise_for_status()
        except Exception as exc:
            return {source: {"status": "unavailable", "error": str(exc)} for source in source_ids}

        results = {}
        for source in source_ids:
            try:
                response = await self.client.get(f"/_data_stream/{source}")
                if response.status_code == 404:
                    results[source] = {"status": "unknown", "reason": "data stream not found"}
                else:
                    response.raise_for_status()
                    latest = await self.client.post(
                        f"/{source}/_search",
                        json={"size": 1, "sort": [{"@timestamp": "desc"}], "_source": ["@timestamp"]},
                    )
                    latest.raise_for_status()
                    hits = latest.json().get("hits", {}).get("hits", [])
                    if not hits:
                        results[source] = {"status": "stale", "reason": "data stream has no events"}
                        continue
                    timestamp = hits[0].get("_source", {}).get("@timestamp")
                    if not timestamp:
                        results[source] = {"status": "stale", "reason": "latest event has no timestamp"}
                        continue
                    observed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    age_seconds = max((datetime.now(UTC) - observed).total_seconds(), 0)
                    results[source] = {
                        "status": "healthy" if age_seconds <= self.source_max_age_seconds else "stale",
                        "cluster": cluster.json().get("status", "unknown"),
                        "latest_event_at": observed.isoformat(),
                        "age_seconds": round(age_seconds, 3),
                    }
            except Exception as exc:
                results[source] = {"status": "unavailable", "error": str(exc)}
        return results

    async def get_incident(self, alert_id: str, engagement_id: str) -> dict:
        body = {
            "size": 1,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"_id": alert_id}},
                        {"term": {"nightagents.engagement_id": engagement_id}},
                    ]
                }
            },
        }
        return await self._search(".alerts-security.alerts-*", body)

    async def run_catalog_query(self, query_id: str, parameters: dict) -> dict:
        definition = QUERY_CATALOG.get(query_id)
        if definition is None:
            raise ValueError("unknown query catalog id")
        lookback = int(parameters.get("lookback_seconds", definition.maximum_lookback_seconds))
        if lookback < 1 or lookback > definition.maximum_lookback_seconds:
            raise ValueError("lookback outside catalog bounds")
        size = int(parameters.get("size", definition.maximum_results))
        if size < 1 or size > definition.maximum_results:
            raise ValueError("result size outside catalog bounds")
        engagement_id = str(parameters["engagement_id"])
        redirector_id = parameters.get("redirector_id")
        index = str(parameters.get("index", "logs-*-*"))
        if not index.startswith(("logs-", ".alerts-security.alerts-")):
            raise ValueError("index outside allowed data streams")
        since = datetime.now(UTC) - timedelta(seconds=lookback)
        if definition.language == "kql":
            filters = [
                {"range": {"@timestamp": {"gte": since.isoformat()}}},
                {"term": {"nightagents.engagement_id": engagement_id}},
                *definition.fixed_filters,
            ]
            if redirector_id and redirector_id != "unknown":
                filters.append({"term": {"nightagents.redirector_id": str(redirector_id)}})
            return await self._search(index, {"size": size, "query": {"bool": {"filter": filters}}})
        if definition.language == "esql":
            query = (
                f"FROM {index} | WHERE nightagents.engagement_id == ?engagement_id "
                f"AND @timestamp >= NOW() - {lookback} seconds | LIMIT {size}"
            )
            response = await self.client.post(
                "/_query",
                json={"query": query, "params": {"engagement_id": engagement_id}},
            )
            response.raise_for_status()
            return {"status": "success", "query_id": query_id, "data": response.json()}
        raise ValueError(f"catalog language {definition.language} requires a rule result, not ad hoc execution")

    async def run_hunt_queries(self, queries: list[dict], engagement_id: str) -> dict:
        """Batch execution for the hunting agent: up to four catalog-allowlisted
        queries chosen by the model. The catalog remains the allowlist and the
        engagement id is pinned by the platform - the model never supplies it.
        A failing query is reported in place, it does not sink the batch."""
        if not isinstance(queries, list) or len(queries) > 4:
            raise ValueError("hunt batches are limited to 4 catalog queries")
        results = []
        for item in queries:
            query_id = str(item.get("query_id", ""))
            parameters = dict(item.get("parameters") or {})
            parameters["engagement_id"] = engagement_id
            try:
                outcome = await self.run_catalog_query(query_id, parameters)
                results.append({"query_id": query_id, "status": "success", "result": outcome})
            except ValueError as exc:
                results.append({"query_id": query_id, "status": "rejected", "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - one bad query must not sink the hunt
                results.append({"query_id": query_id, "status": "error", "error": str(exc)})
        return {"status": "success", "results": results}

    async def _search(self, index: str, body: dict) -> dict:
        try:
            response = await self.client.post(f"/{index}/_search", json=body)
            response.raise_for_status()
            payload = response.json()
            return {
                "status": "success",
                "events": [
                    hit.get("_source", {}) | {"_id": hit.get("_id")}
                    for hit in payload.get("hits", {}).get("hits", [])
                ],
                "total": payload.get("hits", {}).get("total", {}).get("value", 0),
            }
        except Exception as exc:
            return {"status": "unavailable", "events": [], "error": str(exc)}
