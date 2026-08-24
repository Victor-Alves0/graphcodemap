"""Renderização compacta das respostas (docs/DESIGN.md §3.1/§3.3).

Formato compartilhado entre CLI e servidor MCP: texto denso, envelope de
avisos só quando há desvio, spans `path:linha` para o agente ler o código.
"""

from __future__ import annotations

from . import explain


def warnings(env) -> str:
    if not env.warnings:
        return ""
    return "\n".join(f"⚠ {w}" for w in env.warnings) + "\n\n"


def _tag(r) -> str:
    """Selo compacto por aresta: [confiança · resolver] (ex.: [certain · l1/go])."""
    return f"[{r['confidence']} · {r.get('resolver', 'none')}]"


_CONF_ORDER = {"certain": 0, "inferred": 1, "possible": 2, None: 3}


def _legend(rows) -> list[str]:
    """Traduz cada par (resolver, confiança) PRESENTE numa frase — o `reason`,
    sem repetir por linha. Assim o agente sabe o que cada selo significa."""
    pairs = {(r.get("resolver", "none"), r["confidence"]) for r in rows
             if r.get("confidence") is not None}
    if not pairs:
        return []
    out = ["", "como ler [confiança · resolver]:"]
    for label, conf in sorted(pairs, key=lambda p: (_CONF_ORDER.get(p[1], 9), p[0])):
        out.append(f"  {conf} · {label} — {explain.reason(label, conf)}")
    return out


def _loc(row) -> str:
    return f"{row['path']}:{row['start_line']}"


def find(query: str, rows, env) -> str:
    out = warnings(env)
    if not rows:
        return out + f"nenhum símbolo para '{query}'"
    lines = []
    for r in rows:
        sig = f"  {r['signature']}" if r.get("signature") else ""
        lines.append(f"{r['fqn']}  [{r['kind']}]  {_loc(r)}{sig}")
    return out + "\n".join(lines)


def info(data, env) -> str:
    s, c = data["symbol"], data["counts"]
    lines = [f"{s['fqn']}  [{s['kind']}]  {_loc(s)}-{s['end_line']}"]
    if s.get("signature"):
        lines.append(f"  {s['signature']}")
    if s.get("doc"):
        doc = s["doc"].strip().splitlines()
        lines.append("  doc: " + doc[0] + (" …" if len(doc) > 1 else ""))
    lines.append(f"  callers: {c['callers']}  callees: {c['callees']}  "
                 f"refs: {c['references']}")
    dom = data.get("domain")
    if dom:
        tag = f" «{dom['label']}»" if dom.get("label") else ""
        lines.append(f"  domínio: dom {dom['id']}{tag} ({dom['size']} símbolos)")
    if data["children"]:
        lines.append("  contém:")
        for ch in data["children"]:
            lines.append(f"    {ch['kind']:<9} {ch['name']}  :{ch['start_line']}")
    return warnings(env) + "\n".join(lines)


def repository_tree(data, env) -> str:
    lines = [
        f"repositório físico @ revisão {data.get('revision_id') or '-'} "
        f"[{(data.get('snapshot_hash') or '-')[:12]}]:"
    ]
    if not data["nodes"]:
        lines.append(f"  caminho não encontrado: {data['path'] or '.'}")
        return warnings(env) + "\n".join(lines)
    base_depth = data["nodes"][0]["depth"]
    for node in data["nodes"]:
        depth = node["depth"] - base_depth
        label = node["path"].rsplit("/", 1)[-1] if node["path"] else "."
        suffix = "/" if node["kind"] in {"repository", "directory"} else ""
        state = f" [{node['index_state']}]" if node.get("index_state") else ""
        digest = (f" #{node['content_hash'][:10]}"
                  if node.get("content_hash") else "")
        lines.append(f"  {'  ' * depth}{label}{suffix}{state}{digest}")
    return warnings(env) + "\n".join(lines)


def graph_history(rows, env) -> str:
    if not rows:
        return warnings(env) + "nenhuma revisão do grafo registrada"
    lines = []
    for revision in rows:
        git = (revision.get("git_commit") or "sem-git")[:12]
        dirty = "+dirty" if revision.get("git_dirty") else ""
        lines.append(
            f"r{revision['id']} {revision['status']} {revision['trigger']} "
            f"git={git}{dirty} snapshot={revision['source_snapshot_hash'][:12]}"
        )
        lines.append("  " + ", ".join(
            f"{stage['stage']}@{stage['stage_version']}={stage['status']}"
            for stage in revision["stages"]))
    return warnings(env) + "\n".join(lines)


def semantic_coverage(data, env) -> str:
    lines = [
        "cobertura semântica de callsites:",
        f"  certain: {data['certain_sites']}/{data['total_sites']} "
        f"({data['certain_pct']}%)  resolvidos por L1: "
        f"{data['semantic_sites']} ({data['semantic_coverage_pct']}%)",
        f"  fallback L0: {data['fallback_sites']}  "
        f"sem alvo local resolvido: {data['unresolved_sites']}",
    ]
    lines.append(
        "  candidatos locais: "
        f"{data.get('semantic_sites', 0)}/"
        f"{data.get('local_candidate_sites', 0)} resolvidos "
        f"({data.get('local_candidate_coverage_pct', 0.0):.1f}%); "
        f"{data.get('no_local_graph_candidate_sites', 0)} sem candidato "
        "local persistido")
    by_language = data.get("by_language", {})
    if len(by_language) > 1:
        lines.append("  por linguagem:")
        for language, stats in by_language.items():
            lines.append(
                f"    {language}: {stats['semantic_sites']}/"
                f"{stats['total_sites']} total; "
                f"{stats['semantic_sites']}/"
                f"{stats['local_candidate_sites']} candidatos locais "
                f"({stats['local_candidate_coverage_pct']:.1f}%)")
    if data["outcomes"]:
        lines.append("  resultados: " + "  ".join(
            f"{key}={value}" for key, value in data["outcomes"].items()))
    if data["samples"]:
        lines.append("  amostra do que falta:")
        for item in data["samples"]:
            lines.append(
                f"    {item['path']}:{item['line']}:{item['col']} "
                f"{item['callee']} — {item['outcome']}")
    return warnings(env) + "\n".join(lines)


def refs(sym, rows, env) -> str:
    lines = [f"referências a {sym['fqn']} — {len(rows)}:"]
    for r in rows:
        src = r["src_fqn"] or "<módulo>"
        lines.append(f"  {r['site_path']}:{r['line']}  {src}  "
                     f"({r['kind']}) {_tag(r)}")
    if len(rows) >= 60:
        lines.append("  … (truncado no limite; filtre com kind)")
    lines += _legend(rows)
    return warnings(env) + "\n".join(lines)


def calls(sym, rows, env, label: str, direction: str) -> str:
    unresolved = [r for r in rows
                  if r.get("other_fqn") is None and r.get("dst_name")]
    resolved = [r for r in rows if r not in unresolved]
    strong = [r for r in resolved if r["confidence"] == "certain"]
    inferred = [r for r in resolved if r["confidence"] == "inferred"]
    weak = [r for r in resolved if r["confidence"] == "possible"]
    lines = [f"{label} {sym['fqn']} — {len(strong)} confiáveis, "
             f"{len(inferred)} inferidas, {len(weak)} candidatos, "
             f"{len(unresolved)} externas:"]
    for r in [*strong, *inferred]:
        other = r["other_fqn"] or "<módulo>"
        lines.append(f"{'  ' * r['depth']}{r['site_path']}:{r['line']}  "
                     f"{other}  {_tag(r)}")
    if weak:
        # agrega por site: um call site ambíguo casa N candidatos por nome —
        # mostrá-los numa linha (em vez de N) corta tokens sem perder recall
        by_site: dict[tuple, list] = {}
        for r in weak:
            by_site.setdefault((r["site_path"], r["line"]), []).append(
                r["other_fqn"] or "<módulo>")
        lines.append("candidatos por nome (verificar):")
        for (path, line), targets in by_site.items():
            uniq = list(dict.fromkeys(targets))
            shown = ", ".join(uniq[:4]) + (f" +{len(uniq) - 4}" if len(uniq) > 4 else "")
            lines.append(f"  {path}:{line}  {shown}")
    if unresolved and direction == "out":
        # externas/stdlib: só os nomes, agregados — sites individuais são ruído
        counts: dict[str, int] = {}
        for r in unresolved:
            counts[r["dst_name"]] = counts.get(r["dst_name"], 0) + 1
        agg = ", ".join(f"{n}×{c}" if c > 1 else n
                        for n, c in sorted(counts.items()))
        lines.append(f"externas (não resolvidas no repo): {agg}")
    lines += _legend(resolved)
    return warnings(env) + "\n".join(lines)


_IMPACT_CAP = 25


def impact(sym, rows, env) -> str:
    lines = [f"impacto de mudar {sym['fqn']} — {len(rows)} dependente(s), "
             f"por profundidade/importância:"]
    for r in rows[:_IMPACT_CAP]:
        lines.append(f"  [d{r['depth']}] {r['path']}:{r['start_line']}  "
                     f"{r['fqn']} {_tag(r)}")
    if len(rows) > _IMPACT_CAP:
        lines.append(f"  … +{len(rows) - _IMPACT_CAP} (reduza depth para focar)")
    if not rows:
        lines.append("  nenhum dependente conhecido no repo.")
    lines += _legend(rows[:_IMPACT_CAP])
    return warnings(env) + "\n".join(lines)


def change_impact(data, env) -> str:
    lines = [f"impacto da mudança — {data['n_changed']} símbolo(s) alterado(s) em "
             f"{len(data['changed_files'])} arquivo(s) → {data['n_impacted']} "
             f"dependente(s):"]
    if data["changed_symbols"]:
        alt = ", ".join(f"{c['fqn']}" for c in data["changed_symbols"][:8])
        lines.append(f"  alterados: {alt}"
                     + (f" +{len(data['changed_symbols']) - 8}"
                        if len(data["changed_symbols"]) > 8 else ""))
    for r in data["impacted"][:_IMPACT_CAP]:
        lines.append(f"  [d{r['depth']}] {r['path']}:{r['start_line']}  "
                     f"{r['fqn']} {_tag(r)}")
    if data["n_impacted"] > _IMPACT_CAP:
        lines.append(f"  … +{data['n_impacted'] - _IMPACT_CAP}")
    if not data["impacted"]:
        lines.append("  nenhum dependente conhecido no repo.")
    lines += _legend(data["impacted"][:_IMPACT_CAP])
    return warnings(env) + "\n".join(lines)


def affected_modules(data, env) -> str:
    lines = [f"módulos afetados por {len(data['changed_files'])} arquivo(s) "
             f"alterado(s) — {data['n_modules']}:"]
    for m in data["modules"][:_IMPACT_CAP]:
        lines.append(f"  [d{m['min_depth']}] {m['path']}  ({m['count']} símbolo(s))")
        lines.append(f"      {', '.join(m['symbols'])}")
    if not data["modules"]:
        lines.append("  nenhum módulo dependente conhecido.")
    return warnings(env) + "\n".join(lines)


def related_tests(data, env) -> str:
    s = data["symbol"]
    lines = [f"testes que exercitam {s['fqn']} — {data['n']}:"]
    for t in data["tests"]:
        lines.append(f"  {t['path']}:{t['line']}  {t['test']} {_tag(t)}")
    if not data["tests"]:
        lines.append("  nenhum teste conhecido chega a este símbolo "
                     "(pode haver cobertura dinâmica/indireta).")
    lines += _legend(data["tests"])
    return warnings(env) + "\n".join(lines)


def explain_symbol(data, env) -> str:
    s = data["symbol"]
    c = data["counts"]
    lines = [f"{s['fqn']}  [{s['kind']}]  {_loc(s)}"]
    if s.get("signature"):
        lines.append(f"  assinatura: {s['signature']}")
    if s.get("doc"):
        lines.append(f"  doc: {s['doc'].splitlines()[0][:120]}")
    lines.append(f"  usos: {c['callers']} callers, {c['callees']} callees, "
                 f"{c['references']} refs")
    dom = data.get("domain")
    if dom:
        tag = f" «{dom['label']}»" if dom.get("label") else ""
        lines.append(f"  domínio: dom {dom['id']}{tag}")
    if data["callers"]:
        lines.append("  chamado por: "
                     + ", ".join(x["fqn"] for x in data["callers"]))
    if data["callees"]:
        lines.append("  chama: " + ", ".join(x["fqn"] for x in data["callees"]))
    if data["children"]:
        lines.append("  contém: "
                     + ", ".join(f"{ch['name']}({ch['kind']})"
                                 for ch in data["children"][:10]))
    return warnings(env) + "\n".join(lines)


def suggest_files(data, env) -> str:
    lines = [f"arquivos sugeridos para: “{data['task']}” "
             f"(termos: {', '.join(data['tokens']) or '—'}):"]
    for f in data["files"]:
        lines.append(f"  {f['path']}  (score {f['score']})")
        if f["matches"]:
            lines.append(f"      via: {', '.join(f['matches'])}")
    if not data["files"]:
        lines.append("  nada casou — refine os termos da tarefa.")
    return warnings(env) + "\n".join(lines)


def ego(data, env) -> str:
    s = data["symbol"]
    lines = [f"ego-graph de {s['fqn']}  [{s['kind']}]  {_loc(s)}"]
    if data["parent"]:
        lines.append(f"  contido em: {data['parent']}")
    if data["children"]:
        names = ", ".join(f"{c['name']}({c['kind']})" for c in data["children"])
        lines.append(f"  contém: {names}")
    if data["in"]:
        lines.append("  ← entrada:")
        for r in data["in"]:
            other = r["other_fqn"] or "<módulo>"
            lines.append(f"    {r['kind']:<9} {other}  "
                         f"{r['site_path']}:{r['line']} {_tag(r)}")
    if data["out"]:
        lines.append("  → saída:")
        for r in data["out"]:
            other = r["other_fqn"] or f"?{r['dst_name']}"
            lines.append(f"    {r['kind']:<9} {other}  :{r['line']} {_tag(r)}")
    lines += _legend(data["in"] + data["out"])
    return warnings(env) + "\n".join(lines)


def overview(entries, env) -> str:
    lines = ["mapa do repo (top símbolos por importância no grafo):"]
    for e in entries:
        lines.append(e["path"])
        for s in e["symbols"]:
            sig = s["signature"] or s["name"]
            if len(sig) > 100:
                sig = sig[:97] + "…"
            lines.append(f"  {s['kind']:<9} {sig}  :{s['start_line']}")
    return warnings(env) + "\n".join(lines)


def dataflow(data, env) -> str:
    fn = data["function"]
    head = f"fluxo de dados de {fn['fqn']}  [{fn['kind']}]  {_loc(fn)}"
    if not data["supported"]:
        return warnings(env) + head + "\n  (linguagem sem análise de fluxo)"
    lines = [head]
    for p in data["params"]:
        tag = " → alcança o retorno" if p["reaches_return"] else ""
        lines.append(f"  parâmetro '{p['name']}'{tag}:")
        if not p["sinks"]:
            lines.append("    (não alcança nenhuma chamada rastreável)")
        for s in p["sinks"]:
            indent = "    " + "  " * (s["depth"] - 1)
            arg = f"arg#{s['arg_index']}" if s["arg_index"] >= 0 else "kwarg"
            if s["resolved"]:
                tgt = f"{s['callee_fqn']} [{s['confidence']}]"
                loc = f"{s['callee_path']}:{s['callee_line']}"
            else:
                tgt = f"{s['callee_name']} (externa/não resolvida)"
                loc = f"{s['site_path']}:{s['line']}"
            lines.append(f"{indent}→ {tgt}  ({arg}, via {s['via']})  {loc}")
    return warnings(env) + "\n".join(lines)


_TRUST = {
    "certain": "confiança ALTA — cadeia resolvida semanticamente (L1); "
               "pode confiar sem reler o código.",
    "inferred": "confiança MÉDIA — arestas por nome único; provável, confira o "
                "elo mais fraco se for crítico.",
    "possible": "confiança BAIXA — palpite por nome (sem L1 nesta linguagem); "
                "verifique lendo o código.",
}


def reaches(sym, data, env) -> str:
    via = data.get("via")
    head = (f"reachability de {sym['fqn']} → sink '{data['sink']}'"
            + (f" (validador: {via})" if via else "") + ":")
    lines = [head]
    if data.get("limit_hit"):
        lines.append(f"  [parcial: parou em '{data['limit_hit']}' — "
                     f"{data.get('elapsed_ms', 0)}ms]")
    if not data["paths"]:
        lines.append(f"  nenhum caminho alcança um sink '{data['sink']}' "
                     f"(profundidade/arestas 'calls').")
    for p in data["paths"]:
        hops = " → ".join(f.split(".")[-1] for f in p["chain"])
        conf = p["confidence"]
        lines.append(f"  ⇒ SINK {p['sink_call']}  [{conf}]  "
                     f"{p['site_path']}:{p['line']}")
        lines.append(f"    caminho ({len(p['chain'])} nós): {hops}")
        lines.append(f"    {_TRUST.get(conf, '')}")
        if via is not None:
            verdict = (f"{via} PRESENTE no caminho" if p["via_present"]
                       else f"⚠ {via} AUSENTE no caminho — nada valida antes do sink")
            lines.append(f"    validação: {verdict}")
    return warnings(env) + "\n".join(lines)


def taint(data, env) -> str:
    fs = data["findings"]
    mode = "entry" if data["mode"] == "entry" else "scan"
    em_teste = sum(1 for f in fs if f.get("in_test"))
    head = (f"taint ({mode}) — {len(fs)} caminho(s) fonte→sink; "
            f"{data['scanned']} função(ões) analisada(s):")
    lines = [head]
    if em_teste:
        # dito no cabeçalho porque muda como o resto se lê: uma suíte que ecoa
        # a requisição de propósito produz achados verdadeiros e sem interesse
        lines.append(f"  [{em_teste} em arquivo de TESTE — listados por último]")
    if data.get("limit_hit"):
        lines.append(f"  [parcial: parou em '{data['limit_hit']}' — "
                     f"{data.get('explored', 0)} nós, {data.get('elapsed_ms', 0)}ms]")
    if not fs:
        lines.append("  nenhum fluxo não-confiável→sink encontrado "
                     "(com as regras atuais).")
    for i, fi in enumerate(fs, 1):
        o, s = fi["origin"], fi["sink"]
        src = (f"{o['what']} em {o['func_fqn']} ({o['path']}:{o['line']})"
               if o["kind"] == "source"
               else f"{o['what']} de {o['func_fqn']} ({o['path']}:{o['line']})")
        sink_fqn = s["callee_fqn"] or s["callee"]
        arg = f"arg#{s['arg_index']}" if s["arg_index"] >= 0 else "kwarg"
        # DOIS eixos: resolução da CHAMADA · evidência do FLUXO. Juntá-los num
        # rótulo só esconderia que um caminho inteiro 'certain' pode ter fluxo
        # over-aproximado — e vice-versa.
        ev = fi.get("flow_evidence")
        selo = f"[{fi['confidence']}" + (f" · fluxo {ev}" if ev else "")
        selo += " · teste]" if fi.get("in_test") else "]"
        lines.append(f"  [{i}] {selo} {src}")
        lines.append(f"      → SINK {sink_fqn} ({arg}, via {s['via']})  "
                     f"{s['site_path']}:{s['line']}")
        if len(fi["steps"]) > 1:
            hops = " → ".join(st["callee"] for st in fi["steps"])
            lines.append(f"      caminho: {hops}")
    if any(f.get("flow_evidence") == "over-approximated" for f in fs):
        lines += ["", "como ler os dois eixos:",
                  "  confidence — a CHAMADA foi resolvida semanticamente?",
                  "  fluxo      — flow-sensitive: ordem e redefinição foram "
                  "consideradas; over-approximated: algum trecho do caminho "
                  "ignora ordem, então o fluxo pode não ser real."]
    return warnings(env) + "\n".join(lines)


def communities(items, meta, env) -> str:
    head = (f"domínios do repo (comunidades do grafo) — {meta['total']} no total, "
            f"{meta['assigned']} símbolos agrupados; mostrando {meta['shown']} "
            f"(size ≥ {meta['min_size']}):")
    lines = [head]
    for c in items:
        title = f"[dom {c['id']}] {c['size']} símbolos"
        if c.get("label"):
            title += f" — {c['label']}"
        lines.append(title)
        if c.get("summary"):
            lines.append(f"    {c['summary']}")
        if c["top_files"]:
            fs = ", ".join(f"{f['path']}×{f['c']}" for f in c["top_files"])
            lines.append(f"    arquivos: {fs}")
        if c["top_symbols"]:
            ss = ", ".join(f"{s['fqn']}" for s in c["top_symbols"])
            lines.append(f"    hubs: {ss}")
        if not c.get("label"):
            lines.append(f"    (rotule com: describe domain:{c['id']})")
    return warnings(env) + "\n".join(lines)


def describe(data, env) -> str:
    import datetime

    when = datetime.datetime.fromtimestamp(data["generated_at"]).strftime("%Y-%m-%d %H:%M") \
        if data.get("generated_at") else "?"
    state = "fresh" if data["fresh"] else "STALE"
    label = f" «{data['label']}»" if data.get("label") else ""
    header = (f"descrição de {data['target']} ({data['scope']}){label} — "
              f"{data['model']}, {when} [{state}]")
    u = data.get("usage")
    cost = (f"\ncusto: {u['total_tokens']} tokens "
            f"(prompt {u['prompt_tokens']} + completion {u['completion_tokens']}, "
            f"{u['calls']} chamada(s))") if u else ""
    return warnings(env) + header + cost + "\n\n" + data["content"]


def _age(seconds) -> str:
    if seconds is None:
        return "nunca"
    if seconds < 90:
        return f"{seconds}s atrás"
    if seconds < 5400:
        return f"{seconds // 60}min atrás"
    if seconds < 172800:
        return f"{seconds // 3600}h atrás"
    return f"{seconds // 86400}d atrás"


def doctor(d) -> str:
    lines = [f"saúde do índice — {d.get('root_name', '?')}",
             f"  indexer v{d['indexer_version']}  •  último scan completo: "
             f"{_age(d['last_full_scan_age_s'])}"]

    lifecycle = d.get("l1") or {"status": "not_started"}
    l1_status = lifecycle.get("status", "not_started")

    # sinais de alerta primeiro (o que o usuário precisa ver)
    flags = []
    if l1_status == "running":
        flags.append("L1 em execução — consultas leem o último snapshot publicado")
    elif l1_status == "partial" and not lifecycle.get("published", True):
        flags.append("última tentativa L1 falhou antes da publicação; "
                     "snapshot anterior preservado")
    if d["parse_failed_total"]:
        flags.append(f"{d['parse_failed_total']} arquivo(s) falharam no parse "
                     "(rode com CODEGRAPH_LOG=warning para ver o motivo)")
    if not d["l1_resolvers"]:
        flags.append("nenhum resolver L1 ativo — arestas ficam em 'inferred'/"
                     "'possible' (rode `codegraph setup --install`; para Python, "
                     "`pip install \"graphcodemap[l1]\"` também funciona)")
    for m in d.get("l1_missing", []):
        langs = ", ".join(m["languages"])
        if m.get("reason"):
            action = f"; {m['action']}" if m.get("action") else ""
            flags.append(f"L1 indisponível para {langs}: {m['reason']}{action} "
                         f"— rode `codegraph setup {m['languages'][0]} --install`; "
                         "resolução fica em 'inferred'/'possible'")
        else:
            env = f" (ou defina ${m['env']})" if m.get("env") else ""
            flags.append(f"L1 indisponível para {langs}: '{m['server']}' não está no "
                         f"PATH{env} — rode `codegraph setup "
                         f"{m['languages'][0]} --install`; resolução fica em "
                         "'inferred'/'possible'")
    last_l1 = d.get("l1_last_run") or {}
    if last_l1.get("partial"):
        warnings = last_l1.get("warnings") or []
        detail = warnings[0] if warnings else "consulte a saída de `refine`"
        flags.append(f"última passada L1 terminou parcial: {detail}")
    if (d["call_edges"] and d["certain_pct"] < 20 and d["l1_resolvers"]
            and not last_l1.get("partial")):
        flags.append(f"só {d['certain_pct']}% das chamadas são 'certain' — "
                     "considere rodar `refine` para promover mais arestas")
    if flags:
        lines.append("")
        lines += [f"  ⚠ {f}" for f in flags]

    lines.append("")
    lines.append(f"  arquivos: {d['files']}  símbolos: {d['symbols']}")
    parse = "  ".join(f"{k}={v}" for k, v in sorted(d["parse"].items()))
    lines.append(f"  parse: {parse}")
    conf = "  ".join(f"{k}={v}" for k, v in sorted(d["confidence"].items()))
    lines.append(f"  chamadas: {d['call_edges']} arestas ({conf or 'nenhuma'}); "
                 f"{d['certain_pct']}% certain, {d['dangling']} dangling")
    lines.append(f"  L1 ativo: {', '.join(d['l1_resolvers']) or 'nenhum'}")
    lines.append(f"  L1 lifecycle: {l1_status}")
    langs = "  ".join(f"{k}={v}" for k, v in d["by_language"].items())
    lines.append(f"  linguagens: {langs}")

    if d["parse_failed_sample"]:
        lines.append("")
        lines.append(f"  arquivos com falha ({d['parse_failed_total']}):")
        lines += [f"    {p}" for p in d["parse_failed_sample"]]
        if d["parse_failed_total"] > len(d["parse_failed_sample"]):
            resto = d["parse_failed_total"] - len(d["parse_failed_sample"])
            lines.append(f"    … +{resto}")
    return "\n".join(lines)


def stats(s) -> str:
    total = s["edges"] or 1
    revision = s.get("current_revision_id") or "-"
    l1_status = (s.get("l1") or {}).get("status", "not_started")
    return (f"snapshot físico: {s.get('repository_files', 0)} arquivos / "
            f"{s.get('repository_nodes', 0)} nós  revisão: {revision} "
            f"({s.get('graph_revisions', 0)} registradas)\n"
            f"código indexado: {s['files']} arquivos  símbolos: {s['symbols']}  "
            f"arestas: {s['edges']} ({s['edges_resolved']} resolvidas, "
            f"{s['edges_dangling']} pendentes = "
            f"{100 * s['edges_dangling'] / total:.0f}%)\n"
            f"parse parcial/falho: {s['parse_partial']}  L1: {l1_status}  "
            f"linguagens: {s['by_language']}")


def capabilities(rows, summ, gaps_list) -> str:
    """Mapa 'o que está pronto / o que falta' por linguagem.

    Colunas são as camadas independentes; `·` = não se aplica (marcação/config
    não têm fluxo de dados). A seção final é a lista de trabalho."""
    def mark(v):
        return "sim" if v else "NÃO"

    lines = [
        f"capacidades por linguagem — {summ['languages']} linguagens, "
        f"{summ['dedicated']} com extractor dedicado",
        f"  dataflow/taint: {summ['dataflow']}/{summ['dataflow_applicable']} aplicáveis   "
        f"flow-sensitive: {summ['flow_sensitive']}   "
        f"L1 wired: {summ['l1_wired']} ({summ['l1_validated']} smoke, "
        f"{summ['l1_real_repo']} em repo real)   "
        f"segurança validada: {summ['security_validated']}",
        "",
        f"  {'linguagem':<12} {'extractor':<10} {'dataflow':<9} {'taint':<7} "
        f"{'flow-sens':<10} {'L1':<22} {'evidência':<17} {'nível'}",
        f"  {'-'*12} {'-'*10} {'-'*9} {'-'*7} {'-'*10} {'-'*22} "
        f"{'-'*17} {'-'*20}",
    ]
    for r in rows:
        na = not r["dataflow_applicable"]
        dfl = "·" if na else mark(r["dataflow"])
        tnt = "·" if na else mark(r["taint"])
        flw = "·" if na else mark(r["flow"])
        if not r.get("l1_applicable", True):
            l1 = "·"
        elif not r["l1_wired"]:
            l1 = "NÃO"
        else:
            state = {"real-repo": "repo real", "live-smoke": "smoke",
                     "wired": "wired"}.get(r["l1_evidence"], "wired")
            l1 = f"{state} ({r['l1_server']})"
        evidence = {"real-app": "app real", "labeled-benchmark": "benchmark rotulado",
                    "none": "nenhuma"}[r["security_evidence"]]
        lines.append(f"  {r['language']:<12} {r['extract']:<10} {dfl:<9} {tnt:<7} "
                     f"{flw:<10} {l1:<22} {evidence:<17} {r['product_level']}")

    lines += ["", "  legenda: · = não se aplica; engine = implementado sem corpus "
              "de segurança; validated = possui evidência externa"]
    if gaps_list:
        lines += ["", f"  lacunas ({len(gaps_list)} linguagens):"]
        for g in gaps_list:
            lines.append(f"    {g['language']:<12} falta: {', '.join(g['missing'])}")
    else:
        lines.append("  sem lacunas.")
    return "\n".join(lines)
