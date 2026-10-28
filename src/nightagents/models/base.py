from typing import Protocol


class StructuredModel(Protocol):
    async def invoke(self, role: str, context: dict) -> dict: ...

