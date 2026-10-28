from sqlalchemy.orm import Session

from nightagents.db.repositories import AuditRepository
from nightagents.skills.registry import SkillManifest, SkillRegistry


class SkillSelector:
    def __init__(self, db: Session, registry: SkillRegistry, model):
        self.db = db
        self.registry = registry
        self.model = model
        self.audit = AuditRepository(db)

    async def select(
        self,
        agent: str,
        eligible_skill_ids: list[str],
        engagement_id: str,
        objective: str,
        state_summary: dict,
    ) -> SkillManifest:
        if not eligible_skill_ids:
            raise ValueError("skill selection requires a non-empty allowlist")
        eligible = [self.registry.get(agent, skill_id) for skill_id in eligible_skill_ids]
        response = await self.model.invoke(
            "skill_selector",
            {
                "agent": agent,
                "objective": objective,
                "eligible_skills": [
                    {
                        "skill_id": item.skill_id,
                        "version": item.version,
                        "tools": item.tools,
                        "evaluation_ids": item.evaluation_ids,
                    }
                    for item in eligible
                ],
                "state_summary": state_summary,
            },
        )
        selected_id = str(response.get("skill_id", ""))
        selected = next((item for item in eligible if item.skill_id == selected_id), None)
        if selected is None:
            raise PermissionError("model selected a skill outside the eligible allowlist")
        self.audit.append(
            agent,
            "skill.selected",
            "skill",
            f"{selected.skill_id}@{selected.version}",
            {"eligible": eligible_skill_ids, "objective": objective, "reason": response.get("reason", "")},
            engagement_id,
        )
        self.db.commit()
        return selected
