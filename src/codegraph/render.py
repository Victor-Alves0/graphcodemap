"""Renderização compacta das respostas (docs/DESIGN.md §3.1/§3.3).

Formato compartilhado entre CLI e servidor MCP: texto denso, envelope de
avisos só quando há desvio, spans `path:linha` para o agente ler o código.
"""

from __future__ import annotations


def warnings(env) -> str:
    if not env.warnings:
        return ""
    return "\n".join(f"⚠ {w}" for w in env.warnings) + "\n\n"


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


def refs(sym, rows, env) -> str:
    lines = [f"referências a {sym['fqn']} — {len(rows)}:"]
    for r in rows:
        src = r["src_fqn"] or "<módulo>"
        lines.append(f"  {r['site_path']}:{r['line']}  {src}  "
                     f"({r['kind']}) [{r['confidence']}]")
    if len(rows) >= 60:
        lines.append("  … (truncado no limite; filtre com kind)")
    return warnings(env) + "\n".join(lines)


def calls(sym, rows, env, label: str, direction: str) -> str:
    unresolved = [r for r in rows
                  if r.get("other_fqn") is None and r.get("dst_name")]
    resolved = [r for r in rows if r not in unresolved]
    strong = [r for r in resolved if r["confidence"] != "possible"]
    weak = [r for r in resolved if r["confidence"] == "possible"]
    lines = [f"{label} {sym['fqn']} — {len(strong)} confiáveis, "
             f"{len(weak)} candidatos, {len(unresolved)} externas:"]
    for r in strong:
        other = r["other_fqn"] or "<módulo>"
        lines.append(f"{'  ' * r['depth']}{r['site_path']}:{r['line']}  "
                     f"{other}  [{r['confidence']}]")
    if weak:
        lines.append("candidatos por nome (verificar):")
        for r in weak:
            other = r["other_fqn"] or "<módulo>"
            lines.append(f"  {r['site_path']}:{r['line']}  {other}")
    if unresolved and direction == "out":
        # externas/stdlib: só os nomes, agregados — sites individuais são ruído
        counts: dict[str, int] = {}
        for r in unresolved:
            counts[r["dst_name"]] = counts.get(r["dst_name"], 0) + 1
        agg = ", ".join(f"{n}×{c}" if c > 1 else n
                        for n, c in sorted(counts.items()))
        lines.append(f"externas (não resolvidas no repo): {agg}")
    return warnings(env) + "\n".join(lines)


_IMPACT_CAP = 25


def impact(sym, rows, env) -> str:
    lines = [f"impacto de mudar {sym['fqn']} — {len(rows)} dependente(s), "
             f"por profundidade/importância:"]
    for r in rows[:_IMPACT_CAP]:
        lines.append(f"  [d{r['depth']}] {r['path']}:{r['start_line']}  "
                     f"{r['fqn']} [{r['confidence']}]")
    if len(rows) > _IMPACT_CAP:
        lines.append(f"  … +{len(rows) - _IMPACT_CAP} (reduza depth para focar)")
    if not rows:
        lines.append("  nenhum dependente conhecido no repo.")
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
                         f"{r['site_path']}:{r['line']} [{r['confidence']}]")
    if data["out"]:
        lines.append("  → saída:")
        for r in data["out"]:
            other = r["other_fqn"] or f"?{r['dst_name']}"
            lines.append(f"    {r['kind']:<9} {other}  :{r['line']} [{r['confidence']}]")
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
    head = (f"taint ({mode}) — {len(fs)} caminho(s) fonte→sink; "
            f"{data['scanned']} função(ões) analisada(s):")
    lines = [head]
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
        lines.append(f"  [{i}] [{fi['confidence']}] {src}")
        lines.append(f"      → SINK {sink_fqn} ({arg}, via {s['via']})  "
                     f"{s['site_path']}:{s['line']}")
        if len(fi["steps"]) > 1:
            hops = " → ".join(st["callee"] for st in fi["steps"])
            lines.append(f"      caminho: {hops}")
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
    return warnings(env) + header + "\n\n" + data["content"]


def stats(s) -> str:
    total = s["edges"] or 1
    return (f"arquivos: {s['files']}  símbolos: {s['symbols']}  "
            f"arestas: {s['edges']} ({s['edges_resolved']} resolvidas, "
            f"{s['edges_dangling']} pendentes = "
            f"{100 * s['edges_dangling'] / total:.0f}%)\n"
            f"parse parcial/falho: {s['parse_partial']}  "
            f"linguagens: {s['by_language']}")
