"""Health propagation from L1 into normalized benchmark evidence."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from codegraph import l1
from codegraph.eval.security_bench import run_graphcodemap


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stats: dict) -> dict:
    (tmp_path / "App.java").write_text(
        "class App { static int answer() { return 42; } }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(l1, "refine", lambda _indexer: stats)
    return run_graphcodemap(
        tmp_path,
        refine=True,
        db_path=tmp_path / ".state" / "graph.db",
    )


def test_refine_errors_propagate_and_force_partial_without_truncation(
        tmp_path, monkeypatch):
    root = str(tmp_path.resolve())
    report = _run(tmp_path, monkeypatch, {
        # Errors must fail closed even if an older/custom refiner reports an
        # inconsistent nominal status.
        "status": "complete",
        "partial": False,
        "files": 1,
        "promoted": 3,
        "errors": 1,
        "unavailable": [],
        "warnings": [
            f"FakeResolver ({root}): resolver degradado",
            f"FakeResolver ({root}): ERROR: handshake interrompido",
        ],
        "runs": [{
            "resolver": "FakeResolver",
            "root": root,
            "status": "partial",
            "warnings": ["resolver degradado"],
            "errors": ["handshake interrompido"],
        }],
    })

    assert report["status"] == "partial"
    assert report["invocation"]["status"] == "partial"
    assert report["truncated"] is False
    assert report["errors"] == [
        f"L1 FakeResolver ({root}): handshake interrompido"
    ]
    assert report["warnings"] == [
        f"L1: FakeResolver ({root}): resolver degradado",
        "taint: may-taint estático (over-aproxima) — achados são candidatos "
        "a verificar; ajuste regras em .codegraph/taint.json.",
    ]


def test_explicit_partial_refine_without_errors_remains_distinct_from_truncation(
        tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, {
        "status": "partial",
        "partial": True,
        "files": 1,
        "promoted": 0,
        "errors": 0,
        "unavailable": [],
        "warnings": ["refinamento incompleto"],
        "runs": [],
    })

    assert report["status"] == "partial"
    assert report["truncated"] is False
    assert report["errors"] == []
    assert "L1: refinamento incompleto" in report["warnings"]


def test_unavailable_refine_is_partial_but_not_truncated(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, {
        "status": "partial",
        "partial": True,
        "files": 0,
        "promoted": 0,
        "errors": 0,
        "unavailable": [{
            "languages": ["java"],
            "resolver": "JdtlsResolver",
            "server": "jdtls",
        }],
        "warnings": ["resolver L1 indisponível para java (jdtls)"],
        "runs": [],
    })

    assert report["status"] == "partial"
    assert report["truncated"] is False
    assert report["errors"] == []
    assert "L1: resolver L1 indisponível para java (jdtls)" in report["warnings"]


def test_healthy_zero_promotion_refine_remains_complete_and_schema_valid(
        tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch, {
        "status": "complete",
        "partial": False,
        "files": 1,
        "promoted": 0,
        "errors": 0,
        "unavailable": [],
        "warnings": [],
        "runs": [{
            "resolver": "FakeResolver",
            "root": str(tmp_path.resolve()),
            "status": "complete",
            "warnings": [],
            "errors": [],
        }],
    })

    assert report["status"] == "complete"
    assert report["invocation"]["status"] == "complete"
    assert report["truncated"] is False
    assert report["errors"] == []
    schema = json.loads(
        (Path(__file__).resolve().parents[1]
         / "evals" / "security-report.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(report, schema)
