from __future__ import annotations

import json

import pytest

from codegraph.l1.jdtls_workspace import (
    WORKSPACE_SCHEMA, JdtlsWorkspace, WorkspaceBusy,
)


def _runtime(tmp_path):
    home = tmp_path / "jdtls"
    launcher = home / "plugins" / "org.eclipse.equinox.launcher_1.jar"
    config = home / "config_win"
    java = tmp_path / "jdk" / "bin" / "java.exe"
    launcher.parent.mkdir(parents=True)
    config.mkdir(parents=True)
    java.parent.mkdir(parents=True)
    launcher.write_bytes(b"launcher-v1")
    java.write_bytes(b"java")
    return home, launcher, config, str(java)


def _lease(project, runtime):
    return JdtlsWorkspace(project, *runtime, java_major=21)


def test_workspace_schema_invalidates_pre_metadata_isolation_caches():
    assert WORKSPACE_SCHEMA == 5


def test_workspace_is_reused_after_clean_shutdown(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(cache))
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime(tmp_path)

    first = _lease(project, runtime).acquire()
    marker = first.data / "warm-index"
    marker.write_text("preserved", encoding="utf-8")
    first.release(clean=True)

    second = _lease(project, runtime).acquire()
    try:
        assert second.reused is True
        assert second.recovered is False
        assert marker.read_text(encoding="utf-8") == "preserved"
    finally:
        second.release(clean=True)


def test_build_model_change_invalidates_but_source_change_does_not(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(tmp_path / "cache"))
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime(tmp_path)
    (project / "Main.java").write_text("class Main {}", encoding="utf-8")

    first = _lease(project, runtime).acquire()
    marker = first.data / "warm-index"
    marker.write_text("preserved", encoding="utf-8")
    first.release(clean=True)

    (project / "Main.java").write_text("class Main { int n; }", encoding="utf-8")
    source_only = _lease(project, runtime).acquire()
    assert source_only.reused is True
    source_only.release(clean=True)

    (project / "pom.xml").write_text("<project/>", encoding="utf-8")
    changed_build = _lease(project, runtime).acquire()
    try:
        assert changed_build.invalidated is True
        assert changed_build.reused is False
        assert not marker.exists()
    finally:
        changed_build.release(clean=True)


def test_build_model_change_during_lease_cannot_publish_clean_cache(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(tmp_path / "cache"))
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime(tmp_path)

    first = _lease(project, runtime).acquire()
    marker = first.data / "warm-index"
    marker.write_text("old model", encoding="utf-8")
    (project / "pom.xml").write_text("<project/>", encoding="utf-8")
    first.release(clean=True)

    second = _lease(project, runtime).acquire()
    try:
        assert second.invalidated is True
        assert second.reused is False
        assert not marker.exists()
    finally:
        second.release(clean=True)


def test_running_state_is_crash_recovered(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(tmp_path / "cache"))
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime(tmp_path)

    interrupted = _lease(project, runtime).acquire()
    marker = interrupted.data / "partial-index"
    marker.write_text("unsafe", encoding="utf-8")
    interrupted.release(clean=False)

    recovered = _lease(project, runtime).acquire()
    try:
        assert recovered.recovered is True
        assert recovered.reused is False
        assert not marker.exists()
    finally:
        recovered.release(clean=True)


def test_clean_but_nonreusable_workspace_is_invalidated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(tmp_path / "cache"))
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime(tmp_path)

    first = _lease(project, runtime).acquire()
    marker = first.data / "unsafe-model"
    marker.write_text("m2e apt conflict", encoding="utf-8")
    first.release(clean=True, reusable=False)

    second = _lease(project, runtime).acquire()
    try:
        assert second.invalidated is True
        assert second.reused is False
        assert not marker.exists()
    finally:
        second.release(clean=True)


def test_workspace_lock_rejects_concurrent_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(tmp_path / "cache"))
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACE_LOCK_TIMEOUT", "0")
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime(tmp_path)

    first = _lease(project, runtime).acquire()
    try:
        with pytest.raises(WorkspaceBusy, match="outra análise"):
            _lease(project, runtime).acquire()
    finally:
        first.release(clean=True)


def test_runtime_version_gets_a_distinct_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(tmp_path / "cache"))
    project = tmp_path / "project"
    project.mkdir()
    runtime = _runtime(tmp_path)

    first = _lease(project, runtime).acquire()
    first_path = first.path
    first.release(clean=True)
    runtime[1].write_bytes(b"launcher-v2")

    second = _lease(project, runtime).acquire()
    try:
        assert second.path != first_path
        assert second.reused is False
    finally:
        second.release(clean=True)


def test_workspace_cleanup_is_bounded_and_preserves_active_lease(
        tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACES", str(cache))
    monkeypatch.setenv("CODEGRAPH_JDTLS_WORKSPACE_LIMIT", "2")
    runtime = _runtime(tmp_path)
    active = None
    for number in range(3):
        project = tmp_path / f"project-{number}"
        project.mkdir()
        lease = _lease(project, runtime).acquire()
        if active is not None:
            active.release(clean=True)
        active = lease
    try:
        workspaces = list((cache / "workspaces").iterdir())
        assert len(workspaces) <= 2
        assert active.path in workspaces
        state = json.loads(active.metadata_path.read_text(encoding="utf-8"))
        assert state["status"] == "running"
    finally:
        active.release(clean=True)
