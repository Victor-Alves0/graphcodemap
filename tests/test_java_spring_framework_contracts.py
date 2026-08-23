"""Executable contracts for conservative Spring runtime wiring."""

from __future__ import annotations

import textwrap

from codegraph import CodeGraph


def _graph(tmp_path, files: dict[str, str]) -> CodeGraph:
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    return graph


def _framework_targets(graph: CodeGraph) -> set[str]:
    return {
        row["fqn"]
        for row in graph.indexer.conn.execute(
            "SELECT d.fqn FROM edges e JOIN symbols d ON d.id=e.dst "
            "WHERE e.kind='framework'"
        )
    }


def test_controller_mapping_is_framework_reference_not_fabricated_call(tmp_path):
    graph = _graph(tmp_path, {
        "src/main/java/app/OrdersController.java": """
            package app;
            import org.springframework.web.bind.annotation.GetMapping;
            import org.springframework.web.bind.annotation.RestController;

            @RestController
            class OrdersController {
                @GetMapping("/orders")
                String list() { return "ok"; }
                String helper() { return "internal"; }
            }
        """,
        "src/main/java/app/Plain.java": """
            package app;
            import org.springframework.web.bind.annotation.GetMapping;

            class Plain {
                @GetMapping("/not-a-bean") String accidental() { return "no"; }
            }
        """,
    })
    try:
        assert _framework_targets(graph) == {"app.OrdersController.list"}
        _symbol, refs, _env = graph.references(
            "app.OrdersController.list", kind="framework")
        assert [(row["src_fqn"], row["confidence"], row["resolver"])
                for row in refs] == [
            ("app.OrdersController", "inferred", "l0")
        ]
        assert graph.callers("app.OrdersController.list")[1] == []
        _symbol, impacted, _env = graph.impact("app.OrdersController.list")
        assert any(
            row["fqn"] == "app.OrdersController"
            and row["via"] == "framework"
            and row["confidence"] == "inferred"
            for row in impacted
        )
    finally:
        graph.close()


def test_typed_injection_dispatches_declared_repository_only(tmp_path):
    graph = _graph(tmp_path, {
        "src/main/java/app/OrderRepository.java": """
            package app;
            import org.springframework.stereotype.Repository;

            @Repository
            interface OrderRepository {
                Object findById(String id);
            }
        """,
        "src/main/java/app/OrderService.java": """
            package app;
            import org.springframework.stereotype.Service;

            @Service
            class OrderService {
                private final OrderRepository repository;
                OrderService(OrderRepository repository) {
                    this.repository = repository;
                }
                Object load(String id) { return repository.findById(id); }
                Object generated(String email) {
                    return repository.findByEmail(email);
                }
            }
        """,
    })
    try:
        edge = graph.indexer.conn.execute(
            "SELECT d.fqn, e.confidence, e.resolver FROM edges e "
            "LEFT JOIN symbols d ON d.id=e.dst "
            "WHERE e.dst_name='OrderRepository.findById'"
        ).fetchone()
        assert dict(edge) == {
            "fqn": "app.OrderRepository.findById",
            "confidence": "inferred",
            "resolver": "l0",
        }
        generated = graph.indexer.conn.execute(
            "SELECT dst, confidence, resolver FROM edges "
            "WHERE dst_name='OrderRepository.findByEmail'"
        ).fetchone()
        assert dict(generated) == {
            "dst": None,
            "confidence": "possible",
            "resolver": "l0",
        }
    finally:
        graph.close()


def test_configuration_bean_and_managed_event_are_framework_entries(tmp_path):
    graph = _graph(tmp_path, {
        "src/main/java/app/Wiring.java": """
            package app;
            import org.springframework.context.annotation.Bean;
            import org.springframework.context.annotation.Configuration;

            @Configuration
            class Wiring {
                @Bean Gateway gateway() { return new Gateway(); }
                Gateway helper() { return new Gateway(); }
            }
            class Gateway { void send() {} }
        """,
        "src/main/java/app/Listener.java": """
            package app;
            import org.springframework.context.event.EventListener;
            import org.springframework.scheduling.annotation.Async;
            import org.springframework.stereotype.Component;

            @Component
            class Listener {
                @Async @EventListener void onOrder(OrderCreated event) {}
                @Async void backgroundHelper() {}
            }
            class OrderCreated {}
        """,
        "src/main/java/app/Unmanaged.java": """
            package app;
            import org.springframework.context.event.EventListener;

            class Unmanaged {
                @EventListener void looksLikeCallback(Object event) {}
            }
        """,
    })
    try:
        assert _framework_targets(graph) == {
            "app.Listener.onOrder",
            "app.Wiring.gateway",
        }
        assert "app.Listener.backgroundHelper" not in _framework_targets(graph)
        assert "app.Unmanaged.looksLikeCallback" not in _framework_targets(graph)
    finally:
        graph.close()


def test_same_named_non_spring_annotations_never_create_wiring(tmp_path):
    graph = _graph(tmp_path, {
        "src/main/java/app/Custom.java": """
            package app;
            @interface RestController {}
            @interface GetMapping {}

            @RestController
            class Custom {
                @GetMapping void endpoint() {}
            }
        """,
    })
    try:
        assert _framework_targets(graph) == set()
    finally:
        graph.close()


def test_framework_wiring_is_removed_by_incremental_read_repair(tmp_path):
    source = tmp_path / "Controller.java"
    source.write_text(textwrap.dedent("""
        package app;
        import org.springframework.web.bind.annotation.GetMapping;
        import org.springframework.web.bind.annotation.RestController;
        @RestController class Controller {
            @GetMapping("/x") void endpoint() {}
        }
    """), encoding="utf-8")
    graph = CodeGraph(tmp_path)
    graph.index()
    try:
        assert _framework_targets(graph) == {"app.Controller.endpoint"}
        source.write_text(textwrap.dedent("""
            package app;
            class Controller { void endpoint() {} }
        """), encoding="utf-8")

        _symbol, refs, env = graph.references(
            "app.Controller.endpoint", kind="framework")
        assert refs == []
        assert env.fresh is False
        assert _framework_targets(graph) == set()
    finally:
        graph.close()


def test_framework_entry_span_selects_only_the_annotated_overload(tmp_path):
    graph = _graph(tmp_path, {
        "src/main/java/app/OverloadedController.java": """
            package app;
            import org.springframework.web.bind.annotation.GetMapping;
            import org.springframework.web.bind.annotation.RestController;

            @RestController
            class OverloadedController {
                @GetMapping("/by-id") String find(String id) { return id; }
                String find(int id) { return Integer.toString(id); }
            }
        """,
    })
    try:
        rows = graph.indexer.conn.execute(
            "SELECT d.signature, e.confidence FROM edges e "
            "JOIN symbols d ON d.id=e.dst WHERE e.kind='framework'"
        ).fetchall()
        assert [(row["signature"], row["confidence"]) for row in rows] == [
            ('@GetMapping("/by-id") String find(String id)', "inferred")
        ]
    finally:
        graph.close()


def test_explicit_spring_mvc_parameter_is_a_scan_mode_taint_source(tmp_path):
    graph = _graph(tmp_path, {
        "src/main/java/app/DownloadController.java": """
            package app;
            import java.io.FileInputStream;
            import org.springframework.web.bind.annotation.GetMapping;
            import org.springframework.web.bind.annotation.RequestParam;
            import org.springframework.web.bind.annotation.RestController;

            @RestController
            class DownloadController {
                @GetMapping("/download")
                void download(@RequestParam String path) throws Exception {
                    new FileInputStream(path);
                }

                void internal(@RequestParam String path) throws Exception {
                    new FileInputStream(path);
                }
            }
        """,
        "src/main/java/app/CustomController.java": """
            package app;
            import java.io.FileInputStream;
            @interface RestController {}
            @interface GetMapping {}
            @interface RequestParam {}
            @RestController class CustomController {
                @GetMapping void fake(@RequestParam String path) throws Exception {
                    new FileInputStream(path);
                }
            }
        """,
    })
    try:
        data, _env = graph.taint(max_findings=100)
        spring = [
            finding for finding in data["findings"]
            if finding["origin"]["func_fqn"]
            == "app.DownloadController.download"
            and finding["sink"]["callee"] == "FileInputStream"
        ]
        assert len(spring) == 1
        assert spring[0]["origin"]["what"] == "SpringMVC.RequestParam()"
        assert not any(
            finding["origin"]["func_fqn"].endswith(".internal")
            or finding["origin"]["func_fqn"].endswith(".fake")
            for finding in data["findings"]
        )
    finally:
        graph.close()
