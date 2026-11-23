# -*- coding: utf-8 -*-
"""Lógica do editor visual: validação do horário, avaliação de cada destino possível para uma aula
(mover direto, trocar, mover reorganizando o restante) e busca de soluções com o CP-SAT.

Não depende do Streamlit. As posições são sempre {(oferta, ocorrência): (dia, período)}, com
ocorrência e período começando em 0.
"""
import time
from collections import defaultdict

import pandas as pd

import diagnostico as dg

DIAS = dg.DIAS


class Contexto:
    """Regras obrigatórias do problema, prontas para consulta rápida."""

    def __init__(self, data, blocks, events, n_days, n_periods, day_limits=None):
        self.nd, self.P = int(n_days), int(n_periods)
        self.limits = dict(day_limits or {})
        self.offers = {o["id"]: o for o in dg._offers(data, self.nd, self.P, self.limits)}
        self.blocked = set(blocks)                                   # (professor, dia, período)
        self.ev = {"turma": defaultdict(list), "professor": defaultdict(list)}   # (nome, dia, período) -> [atividades]
        for e in dg._events(events, self.nd, self.P):
            for k in range(e["dur"]):
                for c in e["classes"]:
                    self.ev["turma"][(c, e["d"], e["p"] + k)].append(e["name"])
                for t in e["teachers"]:
                    self.ev["professor"][(t, e["d"], e["p"] + k)].append(e["name"])

    def nome(self, key):
        o = self.offers[key[0]]
        return f"{o['subject']} ({', '.join(o['classes'])})"


def positions(result):
    return {(str(r.oferta), int(r.ocorrencia) - 1): (int(r.dia_idx), int(r.inicio) - 1) for r in result.itertuples()}


def sort_result(df):
    df = df.copy()
    df["_t"] = df["turmas"].apply(lambda xs: ", ".join(xs))
    return df.sort_values(["dia_idx", "inicio", "_t", "disciplina", "oferta"]).drop(columns=["_t"]).reset_index(drop=True)


def apply_positions(result, new_pos):
    """Devolve uma cópia do resultado com as posições de new_pos."""
    df = result.copy()
    for i, r in df.iterrows():
        d, p = new_pos[(str(r.oferta), int(r.ocorrencia) - 1)]
        df.at[i, "dia_idx"], df.at[i, "dia"], df.at[i, "inicio"] = d, DIAS[d], p + 1
    return sort_result(df)


# ----------------------------------------------------------------------------------
# Validação completa
# ----------------------------------------------------------------------------------
def violations(ctx, pos):
    out, cls, tch, per_day = [], defaultdict(list), defaultdict(list), defaultdict(int)
    for key, (d, p) in pos.items():
        o = ctx.offers[key[0]]
        if d < 0 or d >= ctx.nd or p < 0 or p + o["dur"] > ctx.P:
            out.append(f"{ctx.nome(key)} está fora da grade ({DIAS[d] if 0 <= d < 6 else d}, {p + 1}ª aula).")
            continue
        per_day[(key[0], d)] += 1
        if key[1] in o["fixed"] and o["fixed"][key[1]] != (d, p):
            out.append(f"{ctx.nome(key)}: a aula {key[1] + 1} está fixada em outro horário na oferta.")
        for k in range(o["dur"]):
            for c in o["classes"]:
                cls[(c, d, p + k)].append(key)
                for ev in ctx.ev["turma"].get((c, d, p + k), []):
                    out.append(f"{ctx.nome(key)} cai em '{ev}' ({c}, {DIAS[d]}, {p + k + 1}ª aula).")
            for t in o["teachers"]:
                tch[(t, d, p + k)].append(key)
                if (t, d, p + k) in ctx.blocked:
                    out.append(f"{t} está indisponível na {DIAS[d]}, {p + k + 1}ª aula ({ctx.nome(key)}).")
                for ev in ctx.ev["professor"].get((t, d, p + k), []):
                    out.append(f"{ctx.nome(key)} cai em '{ev}' ({t}, {DIAS[d]}, {p + k + 1}ª aula).")
    for kind, cells, label in (("Turma", cls, "tem"), ("Professor(a)", tch, "tem")):
        for (name, d, p), keys in sorted(cells.items(), key=lambda kv: str(kv[0])):
            ks = sorted(set(keys))
            if len(ks) > 1:
                out.append(f"{kind} {name} {label} {len(ks)} aulas ao mesmo tempo ({DIAS[d]}, {p + 1}ª aula): " + " e ".join(ctx.nome(k) for k in ks) + ".")
    for (oid, d), n in sorted(per_day.items()):
        lim = ctx.limits.get(oid)
        if lim and n > lim:
            o = ctx.offers[oid]
            out.append(f"{o['subject']} ({', '.join(o['classes'])}) tem {n} aulas na {DIAS[d]} (limite: {lim} por dia).")
    for oid, o in ctx.offers.items():
        miss = [k for k in range(o["count"]) if (oid, k) not in pos]
        if miss:
            out.append(f"{ctx.nome((oid, 0))}: faltam {len(miss)} aula(s) no horário.")
    return out


def owners(ctx, pos, kind, name):
    """(dia, período) -> [chaves das aulas do recurso observado que cobrem a célula]."""
    m = defaultdict(list)
    for key, (d, p) in pos.items():
        o = ctx.offers[key[0]]
        if name in (o["classes"] if kind == "turma" else o["teachers"]):
            for k in range(o["dur"]):
                m[(d, p + k)].append(key)
    return m


def view_fixed(ctx, kind, name, d, p):
    """Atividade fixa ou bloqueio que ocupa a célula para o recurso observado -> texto ou ''."""
    evs = ctx.ev[kind].get((name, d, p), [])
    if evs:
        return "📌 " + ", ".join(evs)
    if kind == "professor" and (name, d, p) in ctx.blocked:
        return "🚫 indisponível"
    return ""


# ----------------------------------------------------------------------------------
# Destinos possíveis para uma aula
# ----------------------------------------------------------------------------------
def grid_status(ctx, pos, key, kind, name):
    """(dia, período) -> (status, explicação). status: atual | direto | troca | ? | nao"""
    o = ctx.offers[key[0]]
    base = set(violations(ctx, pos))
    cur = pos[key]
    own = owners(ctx, pos, kind, name)
    fixed = key[1] in o["fixed"]
    out = {}
    for d in range(ctx.nd):
        for p in range(ctx.P):
            if (d, p) == cur:
                out[(d, p)] = ("atual", "Posição atual da aula.")
                continue
            if p + o["dur"] > ctx.P:
                out[(d, p)] = ("nao", f"A aula ocupa {o['dur']} períodos e não cabe a partir da {p + 1}ª aula.")
                continue
            if fixed:
                out[(d, p)] = ("nao", "Esta aula está fixada na oferta (coluna 'fixas'). Remova a fixação para movê-la.")
                continue
            new_pos = dict(pos); new_pos[key] = (d, p)
            novas = [v for v in violations(ctx, new_pos) if v not in base]
            if not novas:
                out[(d, p)] = ("direto", "Horário livre: mover não afeta nenhuma outra aula.")
                continue
            occ = [k for k in dict.fromkeys(own.get((d, p), [])) if k != key]
            if len(occ) == 1 and pos[occ[0]] == (d, p):
                k2 = occ[0]; o2 = ctx.offers[k2[0]]
                if o2["dur"] == o["dur"] and k2[1] not in o2["fixed"]:
                    sw = dict(pos); sw[key] = (d, p); sw[k2] = cur
                    if not [v for v in violations(ctx, sw) if v not in base]:
                        out[(d, p)] = ("troca", f"Trocar de lugar com {ctx.nome(k2)}.")
                        continue
            out[(d, p)] = ("?", novas[0])
    return out


def swap_partner(ctx, pos, key, kind, name, d, p):
    own = owners(ctx, pos, kind, name)
    occ = [k for k in dict.fromkeys(own.get((d, p), [])) if k != key]
    return occ[0] if len(occ) == 1 else None


def buscar_reorganizacoes(ctx, data, blocks, events, locked, pos, key, candidatos, limite_por=2.0, total=20.0, progresso=None):
    """Para cada destino em `candidatos`, pergunta ao CP-SAT: existe um horário completo e válido com esta
    aula ali, reorganizando as outras? -> {(dia, período): 'reorg' | 'nao' | '?'}"""
    offers = dg._offers(data, ctx.nd, ctx.P, ctx.limits)
    mdl = dg._Model(offers, list(blocks), dg._events(events, ctx.nd, ctx.P), dict(locked or {}), ctx.nd, ctx.P)
    keys = [k for k in mdl.lit if k != ("trava", key[0], key[1])]          # a trava desta própria aula não vale
    mdl.set_hint(pos)
    t0, out = time.time(), {}
    cands = list(candidatos)
    for i, (d, p) in enumerate(cands):
        if progresso: progresso(i / max(len(cands), 1), f"Testando {DIAS[d]}, {p + 1}ª aula...")
        v = mdl.x.get((key[0], key[1], d, p))
        if v is None:
            out[(d, p)] = "nao"; continue
        if time.time() - t0 > total:
            out[(d, p)] = "?"; continue
        st, _ = mdl.solve(keys, limite_por, extra=[v])
        out[(d, p)] = {"ok": "reorg", "inviavel": "nao"}.get(st, "?")
    if progresso: progresso(1.0, "Busca concluída.")
    return out
