from __future__ import annotations


def fqns(rows):
    return {r["fqn"] for r in rows}


def test_index_extracts_symbols(cg):
    stats = cg.stats()
    assert stats["files"] == 4
    assert stats["parse_partial"] == 0

    rows, _ = cg.find_symbol("TokenService")
    assert "app.auth.TokenService" in fqns(rows)

    rows, _ = cg.find_symbol("validate", kind="method")
    assert "app.auth.TokenService.validate" in fqns(rows)

    rows, _ = cg.find_symbol("formatUser")
    assert "app.utils.formatUser" in fqns(rows)


def test_docstring_and_signature(cg):
    info, _ = cg.symbol_info("app.auth.TokenService.validate")
    s = info["symbol"]
    assert "Confere assinatura" in s["doc"]
    assert s["signature"].startswith("def validate")
    assert s["kind"] == "method"


def test_containment(cg):
    info, _ = cg.symbol_info("app.auth.TokenService")
    names = {c["name"] for c in info["children"]}
    assert {"validate", "_check"} <= names


def test_call_edges_python(cg):
    sym, rows, _ = cg.callers("app.auth.TokenService.validate")
    sites = {(r["site_path"], r["other_fqn"]) for r in rows}
    assert ("app/auth.py", "app.auth.issue_token") in sites
    assert ("app/routes.py", "app.routes.login") in sites


def test_import_traced_call_is_inferred(cg):
    # login() chama issue_token importado de app.auth → alvo único via import
    sym, rows, _ = cg.callers("app.auth.issue_token")
    confs = {r["other_fqn"]: r["confidence"] for r in rows}
    assert confs.get("app.routes.login") == "inferred"


def test_inheritance_ts(cg):
    sym, rows, _ = cg.references("app.utils.BaseView", kind="inherits")
    assert any(r["src_fqn"] == "app.utils.UserView" for r in rows)


def test_cross_language_isolation(cg):
    # a call TS a formatUser resolve dentro do próprio arquivo
    sym, rows, _ = cg.callers("app.utils.formatUser")
    assert any(r["other_fqn"] == "app.utils.UserView.render" for r in rows)


def test_incremental_skip_unchanged(cg):
    stats = cg.index(force=False)
    assert stats["indexed"] == 0  # nada mudou → nada re-indexado


def test_js_assignment_definitions(cg, repo):
    # idioma express: métodos definidos por atribuição
    (repo / "app" / "proto.js").write_text(
        "var Router = require('./router');\n"
        "Router.prototype.handle = function handle(req) {\n"
        "  this.dispatch(req);\n"
        "};\n"
        "Router.prototype.dispatch = function dispatch(req) {\n"
        "  return req;\n"
        "};\n"
        "exports.init = () => {\n"
        "  return 1;\n"
        "};\n", encoding="utf-8")
    cg.index()
    rows, _ = cg.find_symbol("handle", kind="method")
    assert "app.proto.Router.prototype.handle" in {r["fqn"] for r in rows}
    rows, _ = cg.find_symbol("init", kind="function")
    assert "app.proto.init" in {r["fqn"] for r in rows}
    # this.dispatch() dentro de handle resolve para o método por atribuição
    sym, rows, _ = cg.callers("app.proto.Router.prototype.dispatch")
    assert any(r["other_fqn"] == "app.proto.Router.prototype.handle" for r in rows)
