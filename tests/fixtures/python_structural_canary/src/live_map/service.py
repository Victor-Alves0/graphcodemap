from .decorators import traced


class Account:
    def __init__(self, owner: str):
        self._owner = owner

    @property
    def owner(self) -> str:
        return self._owner

    @traced
    def label(self, prefix: str) -> str:
        normalized = prefix.strip()

        def render(suffix: str) -> str:
            return f"{normalized}:{self.owner}{suffix}"

        return render("!")


def build_label(owner: str) -> str:
    account = Account(owner)
    return account.label("acct")
