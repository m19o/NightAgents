class RoleModelRouter:
    def __init__(self, default_model, role_models: dict[str, object]):
        self.default_model = default_model
        self.role_models = role_models
        self.model_name = "role-router"

    async def invoke(self, role: str, context: dict) -> dict:
        model = self.role_models.get(role, self.default_model)
        result = await model.invoke(role, context)
        if not isinstance(result, dict):
            raise ValueError(f"model role returned a non-object result: {role}")
        return result

    def model_for_role(self, role: str) -> str:
        model = self.role_models.get(role, self.default_model)
        return str(getattr(model, "model_name", type(model).__name__))
