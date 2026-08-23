"""Source provenance must survive array/index/control-flow plumbing."""

from __future__ import annotations

from codegraph import CodeGraph


def _findings(tmp_path, load_body: str) -> list[dict]:
    source = f"""
        class TrustedModule {{
            private final java.io.File root;

            TrustedModule(String root) {{
                this.root = new java.io.File(root);
            }}

            java.io.File getPath() {{
                return root;
            }}
        }}

        class Registry {{
            static TrustedModule lookup(String moduleName) {{
                return new TrustedModule("modules/core");
            }}
        }}

        class App {{
            void handle(javax.servlet.http.HttpServletRequest request,
                        boolean preferDefault) throws Exception {{
                String module = request.getParameter("module");
                String[] languages = request.getParameterValues("lang");
                if (languages == null) {{
                    languages = new String[] {{}};
                }}
                if (preferDefault) {{
                    languages = new String[] {{ "en" }};
                }}
                if (languages.length == 0) {{
                    languages = java.util.Arrays.copyOf(
                        languages, languages.length + 1);
                }}
                for (int i = languages.length - 1; i >= 0; i--) {{
                    load(new Object(), module, languages[i]);
                }}
            }}

            static void load(Object context, String moduleName,
                             String language) throws Exception {{
                TrustedModule module = Registry.lookup(moduleName);
                java.io.File base = new java.io.File(module.getPath(), "langs");
                java.io.File candidate = new java.io.File(
                    base, "translation-" + language + ".json");
                {load_body}
            }}
        }}
    """
    (tmp_path / "App.java").write_text(source.strip(), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    try:
        graph.index()
        result, _env = graph.taint(max_findings=100)
        return result["findings"]
    finally:
        graph.close()


def test_array_index_flow_preserves_exact_source_provenance(tmp_path):
    findings = _findings(
        tmp_path,
        "new java.io.FileInputStream(candidate);",
    )
    arg_two_findings = [
        finding
        for finding in findings
        if finding["sink"]["callee"] == "FileInputStream"
        and any(
            step["callee"] == "load" and step["arg_index"] == 2
            for step in finding["steps"]
        )
    ]
    assert arg_two_findings
    assert all(
        finding["origin"]["what"] == "getParameterValues()"
        and finding["origin"].get("argument_literals") == {0: "lang"}
        for finding in arg_two_findings
    )


def test_containment_clears_array_index_flow(tmp_path):
    findings = _findings(
        tmp_path,
        """
        if (!candidate.toPath().normalize().toAbsolutePath().startsWith(
                base.toPath().normalize().toAbsolutePath())) {
            return;
        }
        new java.io.FileInputStream(candidate);
        """,
    )
    arg_two_findings = [
        finding
        for finding in findings
        if finding["sink"]["callee"] == "FileInputStream"
        and any(
            step["callee"] == "load" and step["arg_index"] == 2
            for step in finding["steps"]
        )
    ]
    assert not arg_two_findings
