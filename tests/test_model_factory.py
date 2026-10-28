import httpx

from nightagents.config import Settings
from nightagents.models.factory import build_model
from nightagents.models.local import LocalStructuredModel
from nightagents.models.openai_compatible import OpenAICompatibleModel


def test_factory_defaults_to_local_model() -> None:
    model = build_model(Settings())
    assert isinstance(model, LocalStructuredModel)


def test_factory_selects_openai_compatible_model() -> None:
    model = build_model(Settings(model_api_url="https://model.test", model_name="reasoner"))
    assert isinstance(model, OpenAICompatibleModel)


async def test_openai_compatible_model_requires_json_object() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"phase":"profiling"}'}}]},
        )

    model = OpenAICompatibleModel("https://model.test", "key", "reasoner")
    model_client = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class TestClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = model_client
            super().__init__(*args, **kwargs)

    httpx.AsyncClient = TestClient
    try:
        assert await model.invoke("reasoner", {"event": "x"}) == {"phase": "profiling"}
    finally:
        httpx.AsyncClient = original
