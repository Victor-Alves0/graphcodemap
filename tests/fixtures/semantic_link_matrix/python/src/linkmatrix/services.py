from typing import Protocol


class Runner(Protocol):
    def run(self) -> str: ...


class BaseService:
    def inherited(self) -> str:
        return "base"


class Service(BaseService):
    def direct(self) -> str:
        return "direct"


def imported_helper() -> str:
    return "imported"
