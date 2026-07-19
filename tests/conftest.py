from __future__ import annotations

import textwrap

import pytest

from codegraph import CodeGraph


AUTH_PY = '''
"""Autenticação."""
import hashlib

from app.db import get_session

SECRET = "x"


class TokenService:
    """Valida e emite tokens."""

    def validate(self, token):
        """Confere assinatura do token."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        return self._check(digest)

    def _check(self, digest):
        session = get_session()
        return session is not None and digest


def issue_token(user):
    svc = TokenService()
    return svc.validate(user)
'''

DB_PY = '''
"""Camada de dados."""


def get_session():
    return object()


def close_session(session):
    pass
'''

ROUTES_PY = '''
from app.auth import TokenService, issue_token

service = TokenService()


def login(request):
    token = issue_token(request)
    return service.validate(token)
'''

UTILS_TS = '''
import { login } from "./routes";

/** Formata um usuário. */
export function formatUser(name: string): string {
  return login(name).toUpperCase();
}

export class BaseView {}

export class UserView extends BaseView {
  render(): string {
    return formatUser("x");
  }
}
'''


@pytest.fixture()
def repo(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "auth.py").write_text(textwrap.dedent(AUTH_PY), encoding="utf-8")
    (app / "db.py").write_text(textwrap.dedent(DB_PY), encoding="utf-8")
    (app / "routes.py").write_text(textwrap.dedent(ROUTES_PY), encoding="utf-8")
    (app / "utils.ts").write_text(textwrap.dedent(UTILS_TS), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def cg(repo):
    graph = CodeGraph(repo)
    graph.index()
    yield graph
    graph.close()
