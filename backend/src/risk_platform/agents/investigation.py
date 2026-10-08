"""Investigation Agent interface."""


class InvestigationAgent:
    def initial_plan(self, query: str) -> list[str]:
        """识别为调查当前异常，第一轮需要查什么。"""

        raise NotImplementedError
