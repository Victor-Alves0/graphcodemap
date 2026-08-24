"""Contrato do setup explícito, versionado e seguro de toolchains."""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from pathlib import Path

import pytest

from codegraph import cli, l1, render, setup_tools, tool_config


def test_every_l1_language_has_one_setup_family():
    covered = {
        language for family in setup_tools.TARGET_LANGUAGES.values()
        for language in family
    }
    wired = {
        language for resolver in l1.all_resolvers()
        for language in resolver.languages
    }
    assert covered == wired


def test_every_setup_family_has_an_install_plan(tmp_path):
    for target, languages in setup_tools.TARGET_LANGUAGES.items():
        if not languages and target != "mcp":
            continue
        steps = setup_tools._steps_for(target, tmp_path)  # noqa: SLF001
        assert steps, target


def test_direct_downloads_are_versioned_and_checksum_pinned():
    for name, spec in setup_tools._ARCHIVES.items():  # noqa: SLF001
        assert "latest" not in spec["url"].lower(), name
        assert len(spec["sha256"]) == 64
        int(spec["sha256"], 16)
    for step in setup_tools._steps_for("mcp", Path("tools")):  # noqa: SLF001
        assert "mcp==1.29.0" in step.argv


def test_mcp_discovery_invalidates_a_prior_import_miss(monkeypatch):
    calls = []
    monkeypatch.setattr(setup_tools.importlib, "invalidate_caches",
                        lambda: calls.append(True))
    assert setup_tools._mcp_ready() is True  # noqa: SLF001
    assert calls == [True]


def test_java_plan_installs_jdk21_and_verified_jdtls(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_tools, "_find_runtime", lambda _name: None)
    steps = setup_tools._steps_for("java", tmp_path)  # noqa: SLF001
    rendered = "\n".join(step.display() for step in steps)
    assert "EclipseAdoptium.Temurin.21.JDK" in rendered
    assert "21.0.12.101" in rendered
    assert "jdt-language-server-1.60.0-202606262232.tar.gz" in rendered
    assert "sha256:" in rendered


def test_target_aliases_cover_user_spellings():
    assert setup_tools.normalize_targets(
        ["py", "ts", "c++", "c#", "jvm", "clj"]
    ) == ["python", "javascript", "cpp", "csharp", "java", "clojure"]


def test_setup_detects_only_language_families_present_in_repo(tmp_path):
    (tmp_path / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "noise.go").write_text("package noise\n", encoding="utf-8")

    assert setup_tools.detect_targets(tmp_path) == ["javascript", "java"]


def test_setup_prunes_ignored_directory_before_visiting_files(
        tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "noise.java").write_text("class Noise {}\n", encoding="utf-8")
    visited = []
    original = setup_tools.language_for

    def observe(rel):
        visited.append(rel)
        return original(rel)

    monkeypatch.setattr(setup_tools, "language_for", observe)

    assert setup_tools.detect_targets(tmp_path) == ["python"]
    assert visited == ["app.py"]


def test_doctor_points_missing_java_to_setup_command():
    output = render.doctor({
        "root_name": "repo", "indexer_version": "36",
        "last_full_scan_age_s": 1, "parse_failed_total": 0,
        "l1_resolvers": [], "l1_missing": [{
            "languages": ["java"], "server": "jdtls",
            "reason": "JDTLS ausente", "action": "configure CODEGRAPH_JDTLS",
        }],
        "l1_last_run": None, "call_edges": 0, "certain_pct": 0,
        "files": 1, "symbols": 1, "parse": {"ok": 1}, "confidence": {},
        "dangling": 0, "by_language": {"java": 1},
        "parse_failed_sample": [],
    })
    assert "codegraph setup java --install" in output


def test_setup_without_install_is_read_only(monkeypatch, tmp_path, capsys):
    plan = setup_tools.Plan("java", ("java",), False, "ausente", [])
    monkeypatch.setattr(setup_tools, "build_plans", lambda *_a, **_k: [plan])
    monkeypatch.setattr(setup_tools, "install",
                        lambda *_a, **_k: pytest.fail("não deveria instalar"))

    code = cli.main(["--root", str(tmp_path), "setup", "java"])

    assert code == 1
    assert "para instalar explicitamente" in capsys.readouterr().out


def test_noninteractive_install_requires_yes(monkeypatch, tmp_path, capsys):
    plan = setup_tools.Plan("java", ("java",), False, "ausente", [
        setup_tools.Step("manual", "dummy")])
    monkeypatch.setattr(setup_tools, "build_plans", lambda *_a, **_k: [plan])
    monkeypatch.setattr(setup_tools, "install",
                        lambda *_a, **_k: pytest.fail("não deveria instalar"))

    class NonInteractive:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", NonInteractive())
    code = cli.main([
        "--root", str(tmp_path), "setup", "java", "--install",
    ])

    assert code == 3
    assert "--install --yes" in capsys.readouterr().err


def test_index_install_prepares_before_l1(monkeypatch, tmp_path, capsys):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    prepared = []
    refined = []

    monkeypatch.setattr(cli, "_prepare_repo_tools",
                        lambda args, **_k: prepared.append(args.root) or True)
    monkeypatch.setattr(l1, "refine", lambda _ix: refined.append(True) or {
        "promoted": 0, "files": 1, "status": "complete", "partial": False,
        "errors": 0, "resolvers": ["python"],
    })

    code = cli.main([
        "--root", str(tmp_path), "index", "--install", "--yes",
    ])
    capsys.readouterr()

    assert code == 0
    assert prepared == [str(tmp_path.resolve())]
    assert refined == [True]


def test_saved_tool_config_never_overrides_process_environment(
        monkeypatch, tmp_path):
    config = tmp_path / "toolchains.json"
    monkeypatch.setenv("CODEGRAPH_TOOL_CONFIG", str(config))
    monkeypatch.delenv("CODEGRAPH_JDTLS", raising=False)
    tool_config.save({"CODEGRAPH_JDTLS": "managed", "NOT_ALLOWED": "bad"})

    applied = tool_config.apply_saved_environment()
    assert applied == {"CODEGRAPH_JDTLS": "managed"}
    assert "NOT_ALLOWED" not in os.environ

    monkeypatch.setenv("CODEGRAPH_JDTLS", "explicit")
    assert tool_config.apply_saved_environment() == {}
    assert os.environ["CODEGRAPH_JDTLS"] == "explicit"


def test_verified_archive_rejects_path_traversal(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../escape.exe", b"owned")
    raw = payload.getvalue()
    digest = hashlib.sha256(raw).hexdigest()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    step = setup_tools.Step(
        "download", "malicious", url="https://example.invalid/archive.zip",
        sha256=digest, destination=tmp_path / "tools", archive="zip",
        marker="escape.exe",
    )
    opener = lambda _url, timeout: Response(raw)  # noqa: E731

    with pytest.raises(RuntimeError, match="caminho sai"):
        setup_tools._download_verified(step, tmp_path, opener=opener)  # noqa: SLF001
    assert not (tmp_path / "escape.exe").exists()


def test_windows_batch_launchers_are_started_through_cmd(monkeypatch, tmp_path):
    from codegraph.l1.lsp_base import LspResolver

    launcher = tmp_path / "server.bat"
    launcher.touch()

    class BatchResolver(LspResolver):
        cmd_args = ("stdio",)

        @classmethod
        def _binary(cls):
            return str(launcher)

    monkeypatch.setattr(os, "name", "nt")
    argv = BatchResolver._popen_argv(object.__new__(BatchResolver))
    assert argv[1:4] == ["/d", "/s", "/c"]
    assert "server.bat" in argv[4]
