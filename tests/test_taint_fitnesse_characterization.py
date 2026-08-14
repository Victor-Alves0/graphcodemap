"""Executable characterization of the FitNesse CVE-2024-42499 miss.

The reduced chain is deliberately split into independent primitives.  Passing
tests document independently reusable capabilities without teaching the engine
to recognize one benchmark-shaped fixture.

Run as an executable report with::

    pytest -q -rxX tests/test_taint_fitnesse_characterization.py

FitNesse's real flow has two independently attacker-controlled inputs.  The
shortest route is ``Request.getResource()`` inside ``composeFileName``'s return
expression, assigned to an instance field and consumed by ``filesExist``.  A
second, longer route starts at ``Request.getMap()`` and crosses key iteration
and more instance fields.  Map propagation and the sink are already supported.
Receiver-qualified source classification, nested return-source promotion,
cross-method receiver fields and canonical-containment return summaries are
covered here together with their precision guards.
"""

from __future__ import annotations

import json

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


def test_explicit_fitnesse_request_import_resolves_to_qualified_source(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        package example.web;

        import fitnesse.http.Request;

        class App {
            void handle(Request request) {
                String path = request.getResource();
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings


def test_homonymous_request_type_in_another_package_is_not_a_source(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        package com.acme;

        interface Request {
            String getResource();
            java.util.Map<String, String> getMap();
        }

        class App {
            void handle(Request request) {
                String resource = request.getResource();
                new java.io.File(resource);
                java.util.Map<String, String> entries = request.getMap();
                new java.io.File(entries.get("download"));
            }
        }
        """,
    )
    assert not findings


def test_unresolved_simple_request_type_fails_closed(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        interface Request { String getResource(); }
        class App {
            void handle(Request request) {
                String path = request.getResource();
                new java.io.File(path);
            }
        }
        """,
    )
    assert not findings


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


def test_nested_return_source_respects_ancestor_sanitizer(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        package fitnesse.http;

        interface Request {
            String getResource();
        }

        class App {
            String allowListedPath(String input) {
                return "safe.txt";
            }

            String compose(Request request) {
                return "root/" + allowListedPath(request.getResource());
            }

            void handle(Request request) {
                String path = compose(request);
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings

    # Project configuration is the contract that makes a domain validator a
    # sanitizer.  Re-run in a fresh directory because _file_findings owns its
    # graph and configuration lifecycle.
    clean_dir = tmp_path / "sanitized"
    clean_dir.mkdir()
    (clean_dir / "App.java").write_text(
        """
        package fitnesse.http;
        interface Request { String getResource(); }
        class App {
            String allowListedPath(String input) { return "safe.txt"; }
            String compose(Request request) {
                return "root/" + allowListedPath(request.getResource());
            }
            void handle(Request request) {
                String path = compose(request);
                new java.io.File(path);
            }
        }
        """.strip(),
        encoding="utf-8",
    )
    config_dir = clean_dir / ".codegraph"
    config_dir.mkdir()
    (config_dir / "taint.json").write_text(
        json.dumps({"sanitizers": ["allowListedPath"]}),
        encoding="utf-8",
    )
    graph = CodeGraph(clean_dir)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        assert not [
            finding for finding in data["findings"]
            if finding["sink"]["callee"] == "File"
        ]
    finally:
        graph.close()


def test_nested_source_inside_non_sanitizing_wrapper_stays_tainted(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        package fitnesse.http;

        interface Request { String getResource(); }
        class App {
            String decorate(String input) { return "[" + input + "]"; }
            String compose(Request request) {
                return "root/" + decorate(request.getResource());
            }
            void handle(Request request) {
                String path = compose(request);
                new java.io.File(path);
            }
        }
        """,
    )
    assert findings


def test_source_wrapper_name_is_not_promoted_globally(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        package fitnesse.http;

        interface Request { String getResource(); }

        class DirtyComposer {
            String compose(Request request) {
                return request.getResource();
            }
        }

        class CleanComposer {
            String compose(Request request) {
                return "safe.txt";
            }
        }

        class App {
            void handle(Request request, CleanComposer clean) {
                String path = clean.compose(request);
                new java.io.File(path);
            }
        }
        """,
    )
    assert not findings


def test_curated_fitnesse_names_do_not_taint_other_receiver_types(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class AssetStore { String getResource() { return "logo.svg"; } }
        class Settings {
            java.util.Map<String, String> getMap() {
                return new java.util.HashMap<>();
            }
        }
        class App {
            void handle(AssetStore assets, Settings settings) {
                String bundledAsset = assets.getResource();
                new java.io.File(bundledAsset);
                java.util.Map<String, String> entries = settings.getMap();
                new java.io.File(entries.get("download"));
            }
        }
        """,
    )
    assert not findings


def test_request_values_get_remains_a_framework_source(tmp_path):
    (tmp_path / "app.py").write_text(
        """
def handle(request):
    path = request.values.get("download")
    open(path)
        """.strip(),
        encoding="utf-8",
    )
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        data, _env = graph.taint(max_findings=100)
        assert any(f["sink"]["callee"] == "open" for f in data["findings"])
    finally:
        graph.close()


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


def test_fitnesse_canonical_containment_return_cleans_composed_field(tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        interface Request {
            String getResource();
        }

        class App {
            private String filePath;
            private java.io.File root;

            String composeFileName(Request request, String fileName) {
                try {
                    String basePath = root.getPath();
                    String absoluteBase = new java.io.File(basePath)
                        .getCanonicalPath() + java.io.File.separator;
                    java.nio.file.Path candidate = java.nio.file.Path.of(
                        basePath, request.getResource(), fileName);
                    String absoluteFile = candidate.toFile().getCanonicalPath();
                    if (absoluteFile.startsWith(absoluteBase)) {
                        return candidate.toString();
                    }
                } catch (java.io.IOException ignored) {
                }
                return "";
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
    assert not findings


def test_canonical_containment_with_tainted_base_is_not_a_return_sanitizer(
        tmp_path):
    findings = _file_findings(
        tmp_path,
        """
        class App {
            private String filePath;

            String compose(String basePath, String input) throws Exception {
                String absoluteBase = new java.io.File(basePath)
                    .getCanonicalPath() + java.io.File.separator;
                java.nio.file.Path candidate = java.nio.file.Path.of(
                    basePath, input);
                String absoluteFile = candidate.toFile().getCanonicalPath();
                if (absoluteFile.startsWith(absoluteBase)) {
                    return candidate.toString();
                }
                return "";
            }

            void consume() {
                new java.io.File(this.filePath);
            }

            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String base = request.getParameter("base");
                String input = request.getParameter("path");
                this.filePath = compose(base, input);
                consume();
            }
        }
        """,
    )
    assert findings


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
