"""General contracts distilled from the OpenRefine path-traversal pair.

The important distinction is between data stored in a registry and the key
used to select it.  An untrusted key does not, by itself, taint a trusted
registered value.  Conversely, an untrusted value stored in a map remains
untrusted when read back.  The end-to-end fixtures also keep provisional
``File`` construction separate from the real ``FileInputStream`` I/O sink.
"""

from __future__ import annotations

from codegraph import CodeGraph


def _findings(tmp_path, source: str) -> list[dict]:
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        result, _env = graph.taint(max_findings=100)
        return result["findings"]
    finally:
        graph.close()


def _path_sinks(findings: list[dict]) -> list[dict]:
    return [
        finding
        for finding in findings
        if finding["sink"]["callee"] in {"File", "FileInputStream"}
    ]


def _real_io(findings: list[dict]) -> list[dict]:
    return [
        finding
        for finding in findings
        if finding["sink"]["callee"] == "FileInputStream"
    ]


_REGISTRY_TYPES = """
    class TrustedModule {
        private final java.io.File root;

        TrustedModule(String root) {
            this.root = new java.io.File(root);
        }

        java.io.File getPath() {
            return root;
        }
    }

    class RegistryBase {
        protected final java.util.Map<String, TrustedModule> modulesByName =
            new java.util.HashMap<>();

        RegistryBase() {
            modulesByName.put("core", new TrustedModule("app/modules/core"));
            modulesByName.put("ui", new TrustedModule("app/modules/ui"));
        }
    }

    final class ModuleRegistry extends RegistryBase {
        TrustedModule lookup(String name) {
            return modulesByName.get(name);
        }
    }
"""


def _fixed_registered_base_source() -> str:
    return _REGISTRY_TYPES + """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        ModuleRegistry registry) throws Exception {
                String moduleName = request.getParameter("module");
                String[] languages = request.getParameterValues("lang");
                if (languages == null) {
                    return;
                }
                load(registry, moduleName, languages[0]);
            }

            static void load(ModuleRegistry registry, String moduleName,
                             String language) throws Exception {
                TrustedModule module = registry.lookup(moduleName);
                java.io.File base = new java.io.File(module.getPath(), "langs");
                java.io.File candidate =
                    new java.io.File(base, "translation-" + language + ".json");
                if (!candidate.toPath().normalize().toAbsolutePath().startsWith(
                        base.toPath().normalize().toAbsolutePath())) {
                    return;
                }
                new java.io.FileInputStream(candidate);
            }
        }
        """


def test_registry_lookup_key_does_not_taint_trusted_registered_value(tmp_path):
    findings = _findings(
        tmp_path,
        _REGISTRY_TYPES
        + """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        ModuleRegistry registry) throws Exception {
                String selected = request.getParameter("module");
                TrustedModule module = registry.lookup(selected);
                new java.io.FileInputStream(module.getPath());
            }
        }
        """,
    )
    assert not _real_io(findings)


def test_map_lookup_preserves_taint_from_stored_value(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                java.util.Map<String, String> roots =
                    new java.util.HashMap<>();
                roots.put("selected", request.getParameter("root"));
                String selected = roots.get("selected");
                new java.io.FileInputStream(selected);
            }
        }
        """,
    )
    assert _real_io(findings)


def test_parameter_array_element_reaches_real_io_without_containment(tmp_path):
    findings = _findings(
        tmp_path,
        _REGISTRY_TYPES
        + """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request,
                        ModuleRegistry registry) throws Exception {
                String[] languages = request.getParameterValues("lang");
                if (languages == null) {
                    return;
                }
                load(registry, languages[0]);
            }

            static void load(ModuleRegistry registry, String language)
                    throws Exception {
                java.io.File base =
                    new java.io.File(registry.lookup("core").getPath(), "langs");
                java.io.File candidate =
                    new java.io.File(base, "translation-" + language + ".json");
                new java.io.FileInputStream(candidate);
            }
        }
        """,
    )
    actual_source = [
        finding
        for finding in _real_io(findings)
        if finding["origin"]["what"] == "getParameterValues()"
    ]
    assert actual_source


def test_containment_clears_the_actual_parameter_array_flow(tmp_path):
    findings = _findings(tmp_path, _fixed_registered_base_source())
    actual_source = [
        finding
        for finding in _path_sinks(findings)
        if finding["origin"]["what"] == "getParameterValues()"
    ]
    assert not actual_source


def test_containment_clears_array_child_under_registered_base(tmp_path):
    findings = _findings(tmp_path, _fixed_registered_base_source())
    assert not _path_sinks(findings)


def test_containment_does_not_trust_attacker_controlled_base(tmp_path):
    findings = _findings(
        tmp_path,
        """
        class App {
            void handle(javax.servlet.http.HttpServletRequest request)
                    throws Exception {
                String root = request.getParameter("root");
                String child = request.getParameter("path");
                java.nio.file.Path base = java.nio.file.Path.of(root);
                java.nio.file.Path candidate = base.resolve(child);
                if (!candidate.normalize().toAbsolutePath().startsWith(
                        base.normalize().toAbsolutePath())) {
                    return;
                }
                new java.io.FileInputStream(candidate.toFile());
            }
        }
        """,
    )
    assert _real_io(findings)
