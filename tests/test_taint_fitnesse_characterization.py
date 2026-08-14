"""Executable characterization of the FitNesse CVE-2024-42499 miss.

The reduced chain is deliberately split into independent primitives.  Passing
tests document capabilities that already exist; strict xfails are the exact
gaps an implementation must close without teaching the engine to recognize one
benchmark-shaped fixture.

Run as an executable report with::

    pytest -q -rxX tests/test_taint_fitnesse_characterization.py

FitNesse's real flow has two independently attacker-controlled inputs.  The
shortest route is ``Request.getResource()`` inside ``composeFileName``'s return
expression, assigned to an instance field and consumed by ``filesExist``.  A
second, longer route starts at ``Request.getMap()`` and crosses key iteration
and more instance fields.  Map propagation and the sink are already supported.
The missing primitives are source classification, nested return-source
promotion, and cross-method instance-field flow.

Do not name a local map ``values`` in source-discovery tests.  ``values.get``
currently collides with the framework shortcut for Flask/Django
``request.values.get``; the dedicated precision characterization below keeps
that independent bug visible instead of letting it fake ``Request.getMap``
support.
"""

from __future__ import annotations

import json

import pytest

from codegraph import CodeGraph


def _file_findings(tmp_path, source: str, *, sources: tuple[str, ...] = ()) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    if sources:
        config_dir = tmp_path / ".codegraph"
        config_dir.mkdir()
        (config_dir / "taint.json").write_text(
            json.dumps({"sources": list(sources)}),
            encoding="utf-8",
        )

    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        return [
            finding
            for finding in data["findings"]
            if finding["sink"]["callee"] == "File"
        ]
    finally:
        graph.close()


def test_baseline_servlet_source_reaches_file_sink(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String path = request.getParameter("path");
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings


def test_taint_survives_an_interprocedural_helper_return(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            String compose(String input) {
                String path = "root/" + input;
                return path;
            }

            void handle(javax.servlet.http.HttpServletRequest request) {
                String raw = request.getParameter("path");
                String path = compose(raw);
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings


def test_same_method_instance_field_write_and_read_is_supported(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void handle(javax.servlet.http.HttpServletRequest request) {
                this.path = request.getParameter("path");
                new java.io.File(this.path);
            }
        }
        """,
    )
    assert findings


def test_local_map_put_get_preserves_taint(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request) {
                String raw = request.getParameter("path");
                java.util.Map<String, String> entries = new java.util.HashMap<>();
                entries.put("download", raw);
                String path = entries.get("download");
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings


def test_qualified_override_models_request_get_resource(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        interface Request {
            String getResource();
        }

        class App {
            void handle(Request request) {
                String path = request.getResource();
                new java.io.File(path);
            }
        }
        """,
        sources=("Request.getResource",),
    )
    assert findings


def test_qualified_override_models_request_get_map_and_container_read(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        interface Request {
            java.util.Map<String, String> getMap();
        }

        class App {
            void handle(Request request) {
                java.util.Map<String, String> inputs = request.getMap();
                String path = inputs.get("download");
                new java.io.File(path);
            }
        }
        """,
        sources=("Request.getMap",),
    )
    assert findings


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing primitive: fitnesse.http.Request.getResource is not a curated, "
        "receiver-qualified HTTP source"
    ),
)
def test_fitnesse_get_resource_is_discovered_without_project_override(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        package fitnesse.http;

        interface Request {
            String getResource();
        }

        class App {
            void handle(Request request) {
                String path = request.getResource();
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing primitive: fitnesse.http.Request.getMap is not a curated, "
        "receiver-qualified HTTP source"
    ),
)
def test_fitnesse_get_map_is_discovered_without_project_override(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        package fitnesse.http;

        interface Request {
            java.util.Map<String, String> getMap();
        }

        class App {
            void handle(Request request) {
                java.util.Map<String, String> inputs = request.getMap();
                String path = inputs.get("download");
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings


@pytest.mark.xfail(
    strict=True,
    reason=(
        "precision gap: a local variable named values makes values.get look "
        "like the qualified web source request.values.get"
    ),
)
def test_values_get_is_not_a_framework_source_without_request_receiver(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            void handle(java.util.Map<String, String> values) {
                String localPath = values.get("download");
                new java.io.File(localPath);
            }
        }
        """,
    )
    assert not findings


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing primitive: a configured source nested directly in a helper's "
        "return expression does not promote the helper return"
    ),
)
def test_source_nested_in_helper_return_reaches_caller(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        interface Request {
            String getResource();
        }

        class App {
            String compose(Request request) {
                return "root/" + request.getResource();
            }

            void handle(Request request) {
                String path = compose(request);
                new java.io.File(path);
            }
        }
        """,
        sources=("Request.getResource",),
    )
    assert findings


@pytest.mark.xfail(
    strict=True,
    reason=(
        "missing primitive: taint written to an instance field in one method is "
        "not visible when another method reads that field"
    ),
)
def test_instance_field_taint_crosses_method_boundaries(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String path;

            void capture(javax.servlet.http.HttpServletRequest request) {
                this.path = request.getParameter("path");
            }

            void consume() {
                new java.io.File(this.path);
            }

            void handle(javax.servlet.http.HttpServletRequest request) {
                capture(request);
                consume();
            }
        }
        """,
    )
    assert findings


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the shortest CVE chain still needs both nested return-source promotion "
        "and cross-method instance-field flow after Request.getResource is modeled"
    ),
)
def test_shortest_fitnesse_chain_with_explicit_source_oracle(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        interface Request {
            String getResource();
        }

        class App {
            private String filePath;

            String composeFileName(Request request, String fileName) {
                return "root/" + request.getResource() + "/" + fileName;
            }

            boolean filesExist() {
                return new java.io.File(this.filePath).exists();
            }

            void handle(Request request) {
                this.filePath = composeFileName(request, "history.xml");
                filesExist();
            }
        }
        """,
        sources=("Request.getResource",),
    )
    assert findings


def test_bare_get_resource_rule_taints_an_unrelated_domain_api(tmp_path):
    """Adversarial proof that globally adding the bare method name is unsafe."""
    findings = _file_findings(
        tmp_path,
        """
        class App {
            void handle(AssetStore store) {
                String bundledAsset = store.getResource();
                new java.io.File(bundledAsset);
            }
        }
        """,
        sources=("getResource",),
    )
    assert findings


def test_bare_get_map_rule_taints_an_unrelated_domain_api(tmp_path):
    """Map propagation magnifies a generic-name source collision."""
    findings = _file_findings(
        tmp_path,
        """
        class App {
            void handle(Settings settings) {
                java.util.Map<String, String> entries = settings.getMap();
                String localPath = entries.get("download");
                new java.io.File(localPath);
            }
        }
        """,
        sources=("getMap",),
    )
    assert findings


def test_qualified_request_rule_does_not_taint_unrelated_receiver(tmp_path):
    """Receiver qualification is the precision gate for the source catalog."""
    findings = _file_findings(
        tmp_path,
        """
        interface Request {
            String getResource();
        }

        class App {
            void handle(Request request, AssetStore store) {
                String requestPath = request.getResource();
                new java.io.File(requestPath);
                String bundledAsset = store.getResource();
                new java.io.File(bundledAsset);
            }
        }
        """,
        sources=("Request.getResource",),
    )
    assert len(findings) == 1
