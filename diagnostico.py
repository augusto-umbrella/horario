# -*- coding: utf-8 -*-
"""Diagnóstico de horários inviáveis.

Quando o solver não encontra solução, este módulo descobre QUAIS regras se contradizem.

Como funciona
1. Verificações estáticas (contas simples): carga x horários realmente livres.
2. Modelo CP-SAT só com as regras obrigatórias, em que cada regra pertence a um "grupo"
   controlado por um literal de suposição (assumption):
       turma / professor ....... ninguém em dois lugares ao mesmo tempo
       bloqueio ................ indisponibilidade de um professor em um dia
       atividade ............... atividade fixa da escola (Inglês, provas...)
       fixa .................... aulas fixadas na própria oferta (coluna "fixas")
       trava ................... travas manuais feitas no editor visual
       sincronia ............... grupo de ofertas simultâneas
3. Quando o modelo é inviável, o CP-SAT devolve um "núcleo" (subconjunto de grupos que já
   basta para a contradição). O núcleo é reduzido ao mínimo e depois uma das regras dele é
   relaxada para revelar o PRÓXIMO conflito, e assim por diante até o modelo ficar viável.
4. Conta em quantos conflitos cada regra e cada categoria aparecem -> ranking.

Não depende do Streamlit.
"""
import re
import time
from collections import Counter, defaultdict

import pandas as pd
from ortools.sat.python import cp_model

DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"]
DIAS_MAP = {d: i for i, d in enumerate(DIAS)}

CATEGORIAS = {
    "sinc": "Grupos de sincronização",
    "bloq": "Bloqueios de professores",
    "evento": "Atividades fixas da escola",
    "fixa": "Aulas fixadas nas ofertas",
    "trava": "Travas manuais (editor visual)",
    "limite": "Limite de aulas da mesma disciplina por dia",
    "prof": "Capacidade de professores",
    "turma": "Capacidade de turmas",
}
# Qual regra relaxar primeiro para revelar o próximo conflito: as mais "específicas" (editáveis
# pelo usuário) antes das de capacidade.
PRIORIDADE = {"sinc": 0, "trava": 1, "fixa": 2, "limite": 2.5, "evento": 3, "bloq": 4, "turma": 5, "prof": 6}
SUGESTAO = {
    "sinc": "Revise o grupo: ofertas simultâneas não podem repetir o mesmo professor ou turma, e precisam ter a mesma quantidade e duração.",
    "bloq": "Libere parte da indisponibilidade ou redistribua as aulas do professor.",
    "evento": "Mude o dia/aula da atividade fixa ou retire recursos (turma/professor) dela.",
    "fixa": "Remova ou mude a fixação da coluna 'fixas' da oferta.",
    "trava": "Remova as travas manuais (aba Editor visual) e gere de novo.",
    "limite": "Aumente o limite dessa disciplina (barra lateral > Regras obrigatórias) ou reduza as aulas semanais dela.",
    "prof": "Reduza as aulas do professor, aumente aulas por dia/dias letivos ou libere horários.",
    "turma": "Reduza as aulas da turma, aumente aulas por dia/dias letivos ou libere horários.",
}


def _lst(v):
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v if v is not None else "").split(",") if x.strip() and x.strip().lower() != "nan"]


def _parse_fixed(value, count, nd, P):
    out = {}
    for tok in str(value or "").split(";"):
        m = re.fullmatch(r"\s*(\d+)\s*:\s*([A-Za-zÀ-ÿ]+)\s*-\s*(\d+)\s*", tok)
        if m:
            occ, day, per = int(m.group(1)) - 1, m.group(2).capitalize(), int(m.group(3)) - 1
            if 0 <= occ < count and day in DIAS_MAP:
                out[occ] = (DIAS_MAP[day], per)
    return out


def _offers(data, nd, P, limits=None):
    offers = []
    limits = limits or {}
    for _, r in data.fillna("").iterrows():
        count, dur = int(r.aulas_semana), int(r.duracao)
        offers.append({"id": str(r.id), "classes": _lst(r.turmas), "teachers": _lst(r.professores),
                       "subject": str(r.disciplina), "count": count, "dur": dur,
                       "sync": str(r.sincronizar).strip(), "fixed": _parse_fixed(r.fixas, count, nd, P),
                       "limit": limits.get(str(r.id))})
    return offers


def _events(events, nd, P):
    out = []
    if events is None or len(events) == 0:
        return out
    for _, ev in events.fillna("").iterrows():
        name = str(ev.get("atividade", "")).strip()
        a, dur = pd.to_numeric(ev.get("aula"), errors="coerce"), pd.to_numeric(ev.get("duracao"), errors="coerce")
        day = str(ev.get("dia", "")).strip()
        if not name or pd.isna(a) or pd.isna(dur) or day not in DIAS_MAP or DIAS_MAP[day] >= nd:
            continue
        out.append({"name": name, "d": DIAS_MAP[day], "p": int(a) - 1, "dur": int(dur),
                    "classes": _lst(ev.get("turmas", "")), "teachers": _lst(ev.get("professores", ""))})
    return out


# ----------------------------------------------------------------------------------
# 1) Verificações estáticas
# ----------------------------------------------------------------------------------
def static_findings(offers, blocks, events, nd, P):
    cap = nd * P
    load_t, load_c = defaultdict(int), defaultdict(int)
    for o in offers:
        for t in o["teachers"]:
            load_t[t] += o["count"] * o["dur"]
        for c in o["classes"]:
            load_c[c] += o["count"] * o["dur"]
    busy_t, busy_c = defaultdict(set), defaultdict(set)
    for t, d, p in blocks:
        busy_t[t].add((d, p))
    for e in events:
        for k in range(e["dur"]):
            for t in e["teachers"]:
                busy_t[t].add((e["d"], e["p"] + k))
            for c in e["classes"]:
                busy_c[c].add((e["d"], e["p"] + k))
    out = []
    for kind, loads, busy, label in (("professor", load_t, busy_t, "Professor(a)"), ("turma", load_c, busy_c, "Turma")):
        for name, load in sorted(loads.items()):
            free = cap - len(busy.get(name, ()))
            if load > free:
                why = f"grade de {cap} horários" if free == cap else f"{free} horários livres (a grade tem {cap}, e {cap - free} estão tomados por bloqueios/atividades fixas)"
                out.append(f"{label} {name}: precisa de {load} períodos por semana, mas só tem {why}.")
    # grupos simultâneos que repetem professor/turma
    groups = defaultdict(list)
    for o in offers:
        if o["sync"]:
            groups[o["sync"]].append(o)
    for g, members in sorted(groups.items()):
        cnt = Counter([t for o in members for t in o["teachers"]] + [c for o in members for c in o["classes"]])
        rep = sorted(k for k, n in cnt.items() if n > 1)
        if rep:
            out.append(f"Grupo simultâneo '{g}': {', '.join(rep)} aparece(m) em mais de uma oferta do grupo, mas ninguém pode estar em dois lugares ao mesmo tempo.")
    return out


# ----------------------------------------------------------------------------------
# 2) Modelo com grupos de suposição
# ----------------------------------------------------------------------------------
class _Model:
    def __init__(self, offers, blocks, events, locked, nd, P):
        self.m = cp_model.CpModel()
        self.lit = {}
        m = self.m
        by = {o["id"]: o for o in offers}
        x = {}
        for o in offers:
            for occ in range(o["count"]):
                vs = []
                for d in range(nd):
                    for p in range(P - o["dur"] + 1):
                        v = m.NewBoolVar(f"x{len(x)}"); x[(o["id"], occ, d, p)] = v; vs.append(v)
                if not vs:
                    m.Add(self.L(("fixa", o["id"])) == 0)  # oferta que nem cabe na grade
                    continue
                m.AddExactlyOne(vs)
                if occ in o["fixed"]:
                    pos = (o["id"], occ, *o["fixed"][occ])
                    if pos in x: m.Add(x[pos] >= self.L(("fixa", o["id"])))
                    else: m.Add(self.L(("fixa", o["id"])) == 0)
                if (o["id"], occ) in locked:
                    d, p = locked[(o["id"], occ)]
                    pos = (o["id"], occ, d, p)
                    if pos in x: m.Add(x[pos] >= self.L(("trava", o["id"], occ)))
                    else: m.Add(self.L(("trava", o["id"], occ)) == 0)
        self.x = x
        per_day = defaultdict(list)
        for (oid, occ, d, p), v in x.items():
            per_day[(oid, d)].append(v)
        for o in offers:
            lim = o.get("limit")
            if lim and o["count"] > lim:
                for d in range(nd):
                    vs = per_day.get((o["id"], d), [])
                    if len(vs) > lim:
                        a = self.L(("limite", o["subject"], lim))
                        m.Add(sum(vs) + (len(vs) - lim) * a <= len(vs))
        cells_c, cells_t = defaultdict(list), defaultdict(list)
        for (oid, occ, d, p), v in x.items():
            o = by[oid]
            for k in range(o["dur"]):
                for c in o["classes"]: cells_c[(c, d, p + k)].append(v)
                for t in o["teachers"]: cells_t[(t, d, p + k)].append(v)
        for (c, d, p), vs in cells_c.items():
            if len(vs) > 1: m.Add(sum(vs) + (len(vs) - 1) * self.L(("turma", c)) <= len(vs))
        for (t, d, p), vs in cells_t.items():
            if len(vs) > 1: m.Add(sum(vs) + (len(vs) - 1) * self.L(("prof", t)) <= len(vs))
        for e in events:
            for k in range(e["dur"]):
                for c in e["classes"]:
                    for v in cells_c.get((c, e["d"], e["p"] + k), []): m.Add(v + self.L(("evento", e["name"])) <= 1)
                for t in e["teachers"]:
                    for v in cells_t.get((t, e["d"], e["p"] + k), []): m.Add(v + self.L(("evento", e["name"])) <= 1)
        for t, d, p in blocks:
            for v in cells_t.get((t, d, p), []): m.Add(v + self.L(("bloq", t, d)) <= 1)
        sync = defaultdict(list)
        for o in offers:
            if o["sync"]:
                for occ in range(o["count"]): sync[(o["sync"], occ)].append(o["id"])
        for (g, occ), ids in sync.items():
            for other in ids[1:]:
                for d in range(nd):
                    for p in range(P):
                        a, b = x.get((ids[0], occ, d, p)), x.get((other, occ, d, p))
                        if a is None and b is None: continue
                        if a is None: m.Add(b + self.L(("sinc", g)) <= 1)
                        elif b is None: m.Add(a + self.L(("sinc", g)) <= 1)
                        else:
                            m.Add(a - b + self.L(("sinc", g)) <= 1); m.Add(b - a + self.L(("sinc", g)) <= 1)
        # Restrição redundante de capacidade (carga <= horários livres). Não muda o resultado, mas permite ao
        # solver refutar "pombos demais para poucas casas" na hora, em vez de procurar durante minutos.
        cap = nd * P
        load = {"prof": defaultdict(int), "turma": defaultdict(int)}
        for o in offers:
            for t in o["teachers"]: load["prof"][t] += o["count"] * o["dur"]
            for c in o["classes"]: load["turma"][c] += o["count"] * o["dur"]
        terms = {"prof": defaultdict(dict), "turma": defaultdict(dict)}
        seen = {"prof": defaultdict(set), "turma": defaultdict(set)}
        for t, d, p in blocks:
            if (d, p) not in seen["prof"][t]:
                seen["prof"][t].add((d, p)); k = ("bloq", t, d); terms["prof"][t][k] = terms["prof"][t].get(k, 0) + 1
        for e in events:
            for kk in range(e["dur"]):
                cell = (e["d"], e["p"] + kk)
                if cell[1] >= P: continue
                for kind, names in (("prof", e["teachers"]), ("turma", e["classes"])):
                    for r in names:
                        if cell not in seen[kind][r]:
                            seen[kind][r].add(cell); k = ("evento", e["name"]); terms[kind][r][k] = terms[kind][r].get(k, 0) + 1
        for kind in ("prof", "turma"):
            for r, L in load[kind].items():
                tm = terms[kind].get(r, {})
                big = max(0, L + sum(tm.values()) - cap)
                if big:
                    m.Add(L + sum(n * self.L(k) for k, n in tm.items()) + big * self.L((kind, r)) <= cap + big)
        self.idx = {v.Index(): k for k, v in self.lit.items()}

    def L(self, key):
        if key not in self.lit:
            self.lit[key] = self.m.NewBoolVar("g_" + "_".join(map(str, key)))
        return self.lit[key]

    def set_hint(self, pos):
        """pos = {(oferta, ocorrência): (dia, período)} -> sugere ao solver partir deste horário."""
        self.m.ClearHints()
        for (oid, occ, d, p), v in self.x.items():
            self.m.AddHint(v, 1 if pos.get((oid, occ)) == (d, p) else 0)

    def solve(self, assumed, limit, extra=()):
        """-> ('ok'|'inviavel'|'desconhecido', núcleo[list de chaves]). `extra`: variáveis x que devem valer 1."""
        self.m.ClearAssumptions()
        self.m.AddAssumptions([self.lit[k] for k in assumed] + list(extra))
        s = cp_model.CpSolver()
        s.parameters.max_time_in_seconds = max(0.5, float(limit))
        s.parameters.num_workers = 8
        st = s.Solve(self.m)
        if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return "ok", []
        if st == cp_model.INFEASIBLE:
            return "inviavel", [self.idx[i] for i in s.SufficientAssumptionsForInfeasibility() if i in self.idx]
        return "desconhecido", []


# ----------------------------------------------------------------------------------
# Texto legível
# ----------------------------------------------------------------------------------
def _label(key, offers_by_id):
    k = key[0]
    if k == "prof": return f"Professor(a) {key[1]} (não pode estar em duas aulas ao mesmo tempo)"
    if k == "turma": return f"Turma {key[1]} (não pode ter duas aulas ao mesmo tempo)"
    if k == "bloq": return f"Indisponibilidade de {key[1]} na {DIAS[key[2]]}"
    if k == "evento": return f"Atividade fixa '{key[1]}'"
    if k == "sinc": return f"Grupo simultâneo '{key[1]}'"
    if k == "limite": return f"Limite de {key[2]} aula(s) por dia para {key[1]}"
    if k == "trava": return f"Trava manual da aula {key[2] + 1} da oferta {key[1]} ({offers_by_id.get(key[1], {}).get('subject', '?')} · {', '.join(offers_by_id.get(key[1], {}).get('classes', []))})"
    o = offers_by_id.get(key[1], {})
    desc = f"{o.get('subject', '?')} · {', '.join(o.get('classes', []))}" if o else key[1]
    return f"Aulas fixadas da oferta {key[1]} ({desc})"


def _explain(core, offers_by_id):
    kinds = {k[0] for k in core}
    names = lambda kind: [k[1] for k in core if k[0] == kind]
    if "limite" in kinds:
        subj = ", ".join(sorted({k[1] for k in core if k[0] == "limite"}))
        return f"O limite de aulas por dia de {subj} não é compatível com as outras regras abaixo (carga, bloqueios, atividades fixas ou travas)."
    if "sinc" in kinds and (kinds & {"prof", "turma"}):
        who = ", ".join(names("prof") + names("turma"))
        return (f"As ofertas do grupo simultâneo '{', '.join(names('sinc'))}' precisam acontecer ao mesmo tempo, "
                f"mas {who} participa de mais de uma delas e não pode estar em dois lugares ao mesmo tempo.")
    if kinds <= {"prof", "turma", "bloq", "evento"} and ({"bloq", "evento"} & kinds) and (kinds & {"prof", "turma"}):
        return ("A carga de " + ", ".join(names("prof") + names("turma")) +
                " não cabe nos horários que sobram depois das indisponibilidades/atividades fixas.")
    if kinds == {"bloq"}:
        who = ", ".join(sorted({k[1] for k in core if k[0] == "bloq"}))
        return f"As indisponibilidades cadastradas deixam {who} sem nenhum horário possível para as aulas."
    if kinds <= {"prof", "turma"}:
        return "Carga horária acima do que cabe na grade para " + ", ".join(names("prof") + names("turma")) + "."
    if kinds & {"fixa", "trava"}:
        return "Aulas fixadas/travadas colidem com as demais regras abaixo (ex.: mesma célula ocupada, bloqueio ou atividade fixa)."
    return "Estas regras não podem ser satisfeitas ao mesmo tempo."


# ----------------------------------------------------------------------------------
# 3) Diagnóstico completo
# ----------------------------------------------------------------------------------
def diagnosticar(data, blocks, events, locked, n_days, n_periods, tempo_total=45, max_conflitos=40, limits=None):
    t0 = time.time()
    nd, P = int(n_days), int(n_periods)
    offers = _offers(data, nd, P, limits)
    by = {o["id"]: o for o in offers}
    evs = _events(events, nd, P)
    out = {"estado": "desconhecido", "estaticos": static_findings(offers, list(blocks), evs, nd, P), "conflitos": [],
           "ranking": [], "categorias": [], "relaxadas": [], "parcial": False, "tempo": 0.0}
    mdl = _Model(offers, list(blocks), evs, dict(locked or {}), nd, P)
    keys = list(mdl.lit.keys())
    left = lambda: tempo_total - (time.time() - t0)
    per = lambda cap: min(cap, max(0.5, left()))
    status, core = mdl.solve(keys, per(15))
    if status == "ok":
        out["estado"] = "viavel"
    elif status == "desconhecido":
        out["parcial"] = True
    else:
        out["estado"] = "inviavel"
        assumed, cores = set(keys), []
        while status == "inviavel" and len(cores) < max_conflitos:
            core = set(core)
            for g in sorted(core, key=lambda k: (PRIORIDADE[k[0]], str(k))):   # reduz o núcleo ao mínimo
                if len(core) <= 1 or left() < 2: break
                s2, c2 = mdl.solve(assumed & (core - {g}), per(4))
                if s2 == "inviavel": core = set(c2) & (core - {g}) or (core - {g})
            cores.append(sorted(core, key=lambda k: (PRIORIDADE[k[0]], str(k))))
            drop = min(core, key=lambda k: (PRIORIDADE[k[0]], str(k)))          # relaxa a regra mais "editável"
            assumed.discard(drop); out["relaxadas"].append(drop)
            if left() < 2: out["parcial"] = True; break
            status, core = mdl.solve(assumed, per(15))
            if status == "desconhecido": out["parcial"] = True
        if status == "inviavel" and len(cores) >= max_conflitos: out["parcial"] = True
        freq, cat = Counter(), Counter()
        for c in cores:
            freq.update(c); cat.update({k[0] for k in c})
            out["conflitos"].append({"regras": [_label(k, by) for k in c], "texto": _explain(c, by)})
        out["ranking"] = [{"Regra": _label(k, by), "Categoria": CATEGORIAS[k[0]], "Conflitos": n, "Como corrigir": SUGESTAO[k[0]]}
                          for k, n in sorted(freq.items(), key=lambda kv: (-kv[1], PRIORIDADE[kv[0][0]], str(kv[0])))]
        out["categorias"] = [{"Categoria": CATEGORIAS[k], "Conflitos": n, "de": len(cores)} for k, n in sorted(cat.items(), key=lambda kv: (-kv[1], PRIORIDADE[kv[0]]))]
        out["relaxadas"] = [_label(k, by) for k in out["relaxadas"]]
    out["tempo"] = round(time.time() - t0, 1)
    return out


def to_text(d):
    """Versão em texto do diagnóstico (para baixar ou colar em e-mail)."""
    L = ["DIAGNÓSTICO DE CONFLITOS DO HORÁRIO", ""]
    if d["estado"] == "viavel":
        L.append("As regras obrigatórias não se contradizem; o solver provavelmente esgotou o tempo.")
    if d["estaticos"]:
        L += ["Contas rápidas:"] + ["  - " + x for x in d["estaticos"]] + [""]
    if d["categorias"]:
        L.append("Tipos de regra mais envolvidos:")
        L += [f"  - {r['Categoria']}: {r['Conflitos']} de {r['de']} conflitos" for r in d["categorias"]] + [""]
    if d["ranking"]:
        L.append("Regras que mais aparecem nos conflitos:")
        L += [f"  - ({r['Conflitos']}x) {r['Regra']} -> {r['Como corrigir']}" for r in d["ranking"][:15]] + [""]
    for i, c in enumerate(d["conflitos"], 1):
        L += [f"Conflito {i}: {c['texto']}"] + ["    · " + r for r in c["regras"]]
    if d["parcial"]:
        L += ["", "(Lista possivelmente incompleta: o tempo do diagnóstico acabou.)"]
    return "\n".join(L)
