from dataclasses import dataclass


class BudgetExceededError(RuntimeError):
    pass


@dataclass
class WorkflowBudget:
    maximum_tool_calls: int = 20
    maximum_iterations: int = 4
    maximum_context_tokens: int = 12000
    tool_calls: int = 0
    iterations: int = 0
    context_tokens: int = 0

    def consume_tool(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.maximum_tool_calls:
            raise BudgetExceededError("tool-call budget exceeded")

    def consume_iteration(self) -> None:
        self.iterations += 1
        if self.iterations > self.maximum_iterations:
            raise BudgetExceededError("iteration budget exceeded")

    def set_context_tokens(self, tokens: int) -> None:
        self.context_tokens = tokens
        if tokens > self.maximum_context_tokens:
            raise BudgetExceededError("context budget exceeded")

