from __future__ import annotations

from .base import Ref, Sym


def extract(lang: str, source: bytes, module_fqn: str, tree) -> tuple[list[Sym], list[Ref]]:
    if lang == "python":
        from .python import PythonExtractor

        return PythonExtractor(source, module_fqn).run(tree)
    if lang in ("typescript", "tsx", "javascript"):
        from .tsjs import TsJsExtractor

        return TsJsExtractor(source, module_fqn).run(tree)
    if lang == "rust":
        from .rust import RustExtractor

        return RustExtractor(source, module_fqn).run(tree)
    if lang == "go":
        from .go import GoExtractor

        return GoExtractor(source, module_fqn).run(tree)
    if lang == "java":
        from .java import JavaExtractor

        return JavaExtractor(source, module_fqn).run(tree)
    if lang == "kotlin":
        from .kotlin import KotlinExtractor

        return KotlinExtractor(source, module_fqn).run(tree)
    if lang == "csharp":
        from .csharp import CSharpExtractor

        return CSharpExtractor(source, module_fqn).run(tree)
    if lang in ("c", "cpp", "cuda"):
        from .ccpp import CCppExtractor

        return CCppExtractor(source, module_fqn).run(tree)
    if lang == "php":
        from .php import PhpExtractor

        return PhpExtractor(source, module_fqn).run(tree)
    if lang == "ruby":
        from .ruby import RubyExtractor

        return RubyExtractor(source, module_fqn).run(tree)
    if lang in ("lua", "luau"):
        from .lua import LuaExtractor

        return LuaExtractor(source, module_fqn).run(tree)
    if lang == "swift":
        from .swift import SwiftExtractor

        return SwiftExtractor(source, module_fqn).run(tree)
    if lang == "scala":
        from .scala import ScalaExtractor

        return ScalaExtractor(source, module_fqn).run(tree)
    if lang == "clojure":
        from .clojure import ClojureExtractor

        return ClojureExtractor(source, module_fqn).run(tree)
    if lang == "html":
        from .web import HtmlExtractor

        return HtmlExtractor(source, module_fqn).run(tree)
    if lang in ("css", "scss"):
        from .web import CssExtractor

        return CssExtractor(source, module_fqn).run(tree)
    if lang == "markdown":
        from .docs import MarkdownExtractor

        return MarkdownExtractor(source, module_fqn).run(tree)
    if lang in ("json", "yaml", "toml"):
        from .docs import ConfigExtractor

        return ConfigExtractor(source, module_fqn).run(tree)
    from .generic import GenericExtractor

    return GenericExtractor(source, module_fqn).run(tree)


__all__ = ["Sym", "Ref", "extract"]
