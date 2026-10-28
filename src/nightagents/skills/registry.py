import json
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field, model_validator


class SkillPhase(BaseModel):
    node: str
    kind: str
    tool: str | None = None
    model_role: str | None = None
    arguments: dict = Field(default_factory=dict)
    context_keys: list[str] = Field(default_factory=list)
    maximum_attempts: int = Field(default=1, ge=1, le=5)
    timeout_seconds: float = Field(default=15, ge=0.1, le=300)
    next_node: str | None = None
    transitions: list["TransitionRule"] = Field(default_factory=list)
    output_schema: str | None = None


class TransitionRule(BaseModel):
    path: str
    operator: str = "equals"
    value: object | None = None
    target: str


class SkillManifest(BaseModel):
    skill_id: str
    version: int = Field(ge=1)
    agent: str
    tools: list[str] = Field(default_factory=list)
    maximum_tool_calls: int = Field(default=20, ge=1)
    maximum_context_tokens: int = Field(default=4000, ge=500)
    maximum_iterations: int = Field(default=8, ge=1, le=100)
    deadline_seconds: int = Field(default=45, ge=5, le=3600)
    evaluation_ids: list[str] = Field(default_factory=list)
    status: str = "active"
    phases: list[SkillPhase] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_phases(self):
        nodes = [phase.node for phase in self.phases]
        if len(nodes) != len(set(nodes)):
            raise ValueError("skill phase nodes must be unique")
        for phase in self.phases:
            if phase.kind == "tool" and (not phase.tool or phase.tool not in self.tools):
                raise ValueError(f"phase uses undeclared tool: {phase.node}")
            if phase.kind == "model" and not phase.model_role:
                raise ValueError(f"model phase has no role: {phase.node}")
            if phase.kind not in {"tool", "model"}:
                raise ValueError(f"unsupported phase kind: {phase.kind}")
            targets = [rule.target for rule in phase.transitions]
            if phase.next_node:
                targets.append(phase.next_node)
            invalid = [target for target in targets if target != "$end" and target not in nodes]
            if invalid:
                raise ValueError(f"phase has invalid transition target: {invalid[0]}")
        return self


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def list(self, agent: str | None = None) -> list[SkillManifest]:
        manifests = []
        for path in self.root.glob("*/*/manifest.json"):
            manifest = SkillManifest.model_validate_json(path.read_text())
            if manifest.status == "active" and (agent is None or manifest.agent == agent):
                manifests.append(manifest)
        return sorted(manifests, key=lambda item: item.skill_id)

    def get(self, agent: str, skill_id: str) -> SkillManifest:
        for manifest in self.list(agent):
            if manifest.skill_id == skill_id:
                return manifest
        raise FileNotFoundError(f"skill manifest not found: {agent}/{skill_id}")

    def instructions(self, agent: str, skill_id: str) -> str:
        path = (self.root / agent / skill_id / "SKILL.md").resolve()
        if self.root not in path.parents or not path.is_file():
            raise FileNotFoundError(f"skill instructions not found: {agent}/{skill_id}")
        return path.read_text()

    def schema(self, schema_id: str) -> dict:
        if not schema_id or any(part in {"", ".", ".."} for part in Path(schema_id).parts):
            raise ValueError("invalid skill output schema id")
        path = (self.root / "schemas" / f"{schema_id}.json").resolve()
        schema_root = (self.root / "schemas").resolve()
        if schema_root not in path.parents or not path.is_file():
            raise FileNotFoundError(f"skill output schema not found: {schema_id}")
        return json.loads(path.read_text())

    def validate_output(self, schema_id: str, output: dict) -> None:
        validator = Draft202012Validator(self.schema(schema_id))
        errors = sorted(validator.iter_errors(output), key=lambda error: list(error.path))
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.path) or "$"
            raise ValueError(f"skill output violates {schema_id} at {location}: {error.message}")
