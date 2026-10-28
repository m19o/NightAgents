from pathlib import Path


class SkillNotFoundError(FileNotFoundError):
    pass


class SkillLoader:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def load(self, agent: str, skill: str) -> str:
        path = (self.root / agent / skill / "SKILL.md").resolve()
        if self.root not in path.parents or not path.is_file():
            raise SkillNotFoundError(f"skill not found: {agent}/{skill}")
        return path.read_text()

