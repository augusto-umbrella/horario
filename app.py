import io, re, zipfile, json, os, tempfile, hashlib
from pathlib import Path
from collections import defaultdict, Counter
import pandas as pd
import streamlit as st
from ortools.sat.python import cp_model
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
import pdf_horarios as ph
import diagnostico as dg
import editor_horarios as eh

st.set_page_config(page_title="Gerador de Horários Escolares", page_icon="🗓️", layout="wide")
DIAS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"]
DIAS_MAP = {d: i for i, d in enumerate(DIAS)}
OFFER_COLS = ["id", "turmas", "disciplina", "professores", "aulas_semana", "duracao", "sincronizar", "fixas"]
BLOCK_COLS = ["professor", "dia", "aula", "dia_inteiro"]
SYNC_COLS = ["id", "grupo"]
PREF_COLS = ["professor", "dia", "aula", "preferencia", "prioridade"]
EVENT_COLS = ["atividade", "dia", "aula", "duracao", "turmas", "professores"]
CATALOG_COLS = ["nome"]


def frame(rows, cols):
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


def blank_offerings():
    # Começa vazio: exemplos fixos de professores/turmas não devem competir
    # com os dados reais cadastrados pelo usuário.
    return pd.DataFrame(columns=OFFER_COLS)


def csv_list(v):
    if pd.isna(v) or not str(v).strip(): return []
    return [x.strip() for x in str(v).split(",") if x.strip()]


def safe_int(v, label, minimum=1):
    n = pd.to_numeric(v, errors="coerce")
    if pd.isna(n) or int(n) != n or n < minimum:
        raise ValueError(f"'{label}' deve ser um número inteiro maior ou igual a {minimum}.")
    return int(n)


def parse_fixed(value, offer_id, count, n_periods, days):
    result = {}
    if not str(value).strip(): return result
    for token in str(value).split(";"):
        m = re.fullmatch(r"\s*(\d+)\s*:\s*([A-Za-zÀ-ÿ]+)\s*-\s*(\d+)\s*", token)
        if not m: raise ValueError(f"Fixação inválida na oferta {offer_id}: '{token}'. Use 1:Segunda-2; 2:Quarta-3.")
        occ, day, period = int(m.group(1))-1, m.group(2).capitalize(), int(m.group(3))-1
        if not (0 <= occ < count and day in DIAS_MAP and DIAS_MAP[day] < days and 0 <= period < n_periods):
            raise ValueError(f"Fixação fora dos limites na oferta {offer_id}: '{token}'.")
        result[occ] = (DIAS_MAP[day], period)
    return result


def normalize(data, blocks, n_days, n_periods):
    data = data.copy().fillna("")
    if data.empty: raise ValueError("Cadastre ao menos uma oferta de aula antes de gerar o horário.")
    for c in OFFER_COLS:
        if c not in data: data[c] = ""
    data = data[OFFER_COLS]
    data["id"] = data.id.astype(str).str.strip()
    if (data.id == "").any() or data.id.duplicated().any(): raise ValueError("Cada oferta precisa ter ID único e não vazio.")
    for i, r in data.iterrows():
        if not csv_list(r.turmas) or not csv_list(r.professores) or not str(r.disciplina).strip():
            raise ValueError(f"A oferta {r.id} precisa de turma(s), disciplina e professor(es).")
        count = safe_int(r.aulas_semana, f"aulas_semana da oferta {r.id}")
        dur = safe_int(r.duracao, f"duração da oferta {r.id}")
        if dur not in (1, 2, 3): raise ValueError(f"A oferta {r.id}: duração deve ser 1, 2 ou 3 aulas consecutivas.")
        if dur > n_periods: raise ValueError(f"A duração da oferta {r.id} excede as aulas por dia.")
        data.at[i, "aulas_semana"] = count; data.at[i, "duracao"] = dur
        parse_fixed(r.fixas, r.id, count, n_periods, n_days)
    data["aulas_semana"] = data.aulas_semana.astype(int); data["duracao"] = data.duracao.astype(int)
    teachers = {t for v in data.professores for t in csv_list(v)}
    parsed_blocks = []
    for _, r in blocks.fillna("").iterrows():
        if not str(r.get("professor", "")).strip():
            continue
        teacher = str(r.get("professor", "")).strip()
        day = str(r.get("dia", "")).strip()
        whole_day = str(r.get("dia_inteiro", "")).strip().lower() in {"true", "1", "sim", "yes"}
        if teacher not in teachers or day not in DIAS_MAP or DIAS_MAP[day] >= n_days:
            raise ValueError(f"Bloqueio inválido: {teacher}, {day}.")
        if whole_day:
            parsed_blocks.extend((teacher, DIAS_MAP[day], period) for period in range(n_periods))
        else:
            p = safe_int(r.get("aula", ""), "aula") - 1
            if p >= n_periods:
                raise ValueError(f"Bloqueio inválido: {teacher}, {day}, aula {p+1}.")
            parsed_blocks.append((teacher, DIAS_MAP[day], p))
    return data, parsed_blocks


def prediagnostics(data, blocks, n_days, n_periods, events, locked):
    issues=[]; teachers=defaultdict(int); classes=defaultdict(int)
    for _,r in data.iterrows():
        slots=int(r.aulas_semana)*int(r.duracao)
        for t in csv_list(r.professores): teachers[t]+=slots
        for c in csv_list(r.turmas): classes[c]+=slots
    cap=n_days*n_periods
    for name,load in teachers.items():
        if load>cap: issues.append(f"Professor(a) {name}: {load} períodos semanais solicitados, mas a grade comporta {cap}.")
    for name,load in classes.items():
        if load>cap: issues.append(f"Turma {name}: {load} períodos semanais solicitados, mas a grade comporta {cap}.")
    for group,g in data[data.sincronizar.astype(str).str.strip()!=""].groupby("sincronizar"):
        if g.aulas_semana.nunique()!=1 or g.duracao.nunique()!=1:
            issues.append(f"Grupo simultâneo '{group}': ofertas precisam ter a mesma quantidade semanal e duração.")
    reserved=defaultdict(list)
    for _,r in events.fillna("").iterrows():
        if not str(r.get("atividade", "")).strip(): continue
        try:
            day=str(r.dia); d=DIAS_MAP[day]; p=safe_int(r.aula,"aula")-1; dur=safe_int(r.duracao,"duração")
            if d>=n_days or p+dur>n_periods: issues.append(f"Atividade fixa '{r.atividade}' ultrapassa a grade."); continue
            if not csv_list(r.get("turmas", "")) and not csv_list(r.get("professores", "")):
                issues.append(f"Atividade fixa '{r.atividade}' não reserva nenhum recurso: informe turma(s) e/ou professor(es).")
            for k in range(dur):
                for c in csv_list(r.get("turmas", "")): reserved[("c",c,d,p+k)].append(str(r.atividade))
                for t in csv_list(r.get("professores", "")): reserved[("t",t,d,p+k)].append(str(r.atividade))
        except Exception as e: issues.append(f"Atividade fixa inválida: {e}")
    for key,vals in reserved.items():
        if len(vals)>1: issues.append(f"Conflito entre atividades fixas ({', '.join(vals)}) para {key[1]}, {DIAS[key[2]]}, aula {key[3]+1}.")
    # Detecta sobreposição entre ofertas que já chegam com dia/aula fixados ou travados.
    forced=defaultdict(list)
    for _,r in data.iterrows():
        oid=str(r.id); count=int(r.aulas_semana); dur=int(r.duracao)
        fixed=parse_fixed(r.fixas,oid,count,n_periods,n_days)
        for occ in range(count):
            pos=fixed.get(occ,locked.get((oid,occ)))
            if pos is None: continue
            d,p=pos
            if d>=n_days or p<0 or p+dur>n_periods: continue
            for k in range(dur):
                for c in csv_list(r.turmas): forced[("c",c,d,p+k)].append(f"{oid}/{occ+1}")
                for t in csv_list(r.professores): forced[("t",t,d,p+k)].append(f"{oid}/{occ+1}")
    for key,vals in forced.items():
        if len(vals)>1: issues.append(f"Sobreposição entre aulas fixadas/travadas ({', '.join(vals)}) para {key[1]}, {DIAS[key[2]]}, aula {key[3]+1}.")
    # Verifica se cada ocorrência possui ao menos uma posição que respeite as restrições locais.
    blocked=set(blocks)
    for _,r in data.iterrows():
        oid=str(r.id); count=int(r.aulas_semana); dur=int(r.duracao)
        fixed=parse_fixed(r.fixas,oid,count,n_periods,n_days)
        for occ in range(count):
            options=[]
            for d in range(n_days):
                for p in range(n_periods-dur+1):
                    if occ in fixed and fixed[occ]!=(d,p): continue
                    if (oid,occ) in locked and locked[(oid,occ)]!=(d,p): continue
                    conflict=False
                    for k in range(dur):
                        for t in csv_list(r.professores):
                            if (t,d,p+k) in blocked or reserved.get(("t",t,d,p+k)): conflict=True
                        for c in csv_list(r.turmas):
                            if reserved.get(("c",c,d,p+k)): conflict=True
                    if not conflict: options.append((d,p))
            if not options:
                issues.append(f"Oferta {oid}, ocorrência {occ+1}: sem posição possível após considerar aulas fixas, bloqueios, atividades da escola e travas manuais.")
    return issues


class _Progress(cp_model.CpSolverSolutionCallback):
    """Guarda (segundos, pontuação) de cada solução melhor encontrada, para mostrar se mais tempo ainda ajuda."""
    def __init__(self):
        super().__init__(); self.history = []
    def on_solution_callback(self):
        self.history.append((round(self.WallTime(), 1), float(self.ObjectiveValue())))


def compute_day_limits(data, default, rules, n_days):
    """Limite de aulas da mesma disciplina por dia, por oferta -> ({id_oferta: limite}, avisos).
    rules: {disciplina: limite|None (None = sem limite)}; default: limite|None.
    Se a oferta tem mais aulas do que cabem (ex.: 6 aulas em 5 dias com limite 1), o limite sobe para o mínimo viável."""
    limits, notes = {}, []
    for _, r in data.iterrows():
        subs = [x.strip() for x in str(r.disciplina).split("/") if x.strip()] or [str(r.disciplina)]
        cands = [(rules[x] if x in rules else default) for x in subs]
        cands = [c for c in cands if c]
        if not cands:
            continue
        lim, count = min(cands), int(r.aulas_semana)
        need = -(-count // int(n_days))
        if lim < need:
            notes.append(f"{r.disciplina} ({r.turmas}): {count} aulas em {n_days} dias não cabem com limite de {lim} por dia; usei {need}.")
            lim = need
        if count > lim:
            limits[str(r.id)] = lim
    return limits, notes


def build_solve(data, blocks, events, prefs, locked, n_days, n_periods, time_limit, weights, day_limits=None, reference=None):
    model=cp_model.CpModel(); x={}; by={}; sync=defaultdict(list)
    # Reserve fixed school activities.
    event_cells=defaultdict(list)
    for idx,r in events.fillna("").iterrows():
        if not str(r.get("atividade","")).strip(): continue
        d=DIAS_MAP[str(r.dia)]; p=safe_int(r.aula,"aula")-1; dur=safe_int(r.duracao,"duração")
        if d>=n_days or p+dur>n_periods: raise ValueError(f"Atividade fixa '{r.atividade}' fora da grade.")
        for k in range(dur):
            for c in csv_list(r.turmas): event_cells[("c",c,d,p+k)].append(str(r.atividade))
            for t in csv_list(r.professores): event_cells[("t",t,d,p+k)].append(str(r.atividade))
    for _,r in data.iterrows():
        oid=str(r.id); count=int(r.aulas_semana); dur=int(r.duracao); fixed=parse_fixed(r.fixas,oid,count,n_periods,n_days)
        off={"id":oid,"classes":csv_list(r.turmas),"teachers":csv_list(r.professores),"subject":str(r.disciplina),"count":count,"duration":dur,"sync":str(r.sincronizar).strip()}; by[oid]=off
        for occ in range(count):
            candidates=[]
            for d in range(n_days):
                for p in range(n_periods-dur+1):
                    if occ in fixed and fixed[occ]!=(d,p): continue
                    if (oid,occ) in locked and locked[(oid,occ)]!=(d,p): continue
                    candidates.append((d,p))
            if not candidates: raise ValueError(f"Oferta {oid}, ocorrência {occ+1}: nenhuma posição possível após aplicar fixações/travas.")
            vs=[]
            for d,p in candidates:
                v=model.NewBoolVar(f"x_{oid}_{occ}_{d}_{p}"); x[(oid,occ,d,p)]=v; vs.append(v)
            model.AddExactlyOne(vs)
            if off["sync"]: sync[(off["sync"],occ)].append((oid,occ))
    for (group,occ),members in sync.items():
        if len(members)<2: continue
        base=members[0]
        for other in members[1:]:
            for d in range(n_days):
                for p in range(n_periods):
                    a=x.get((base[0],base[1],d,p)); b=x.get((other[0],other[1],d,p))
                    if a is None and b is not None: model.Add(b==0)
                    elif b is None and a is not None: model.Add(a==0)
                    elif a is not None and b is not None: model.Add(a==b)
    per_off_day=defaultdict(list)
    for (oid,occ,d,p),v in x.items(): per_off_day[(oid,d)].append(v)
    for oid,lim in (day_limits or {}).items():
        if oid in by and by[oid]["count"]>lim:
            for d in range(n_days):
                vs=per_off_day.get((oid,d),[])
                if len(vs)>lim: model.Add(sum(vs)<=lim)
    if reference:
        for (oid,occ,d,p),v in x.items(): model.AddHint(v,1 if reference.get((oid,occ))==(d,p) else 0)
    class_occ=defaultdict(list); teacher_occ=defaultdict(list)
    for (oid,occ,d,p),v in x.items():
        off=by[oid]
        for k in range(off["duration"]):
            for c in off["classes"]: class_occ[(c,d,p+k)].append(v)
            for t in off["teachers"]: teacher_occ[(t,d,p+k)].append(v)
    for key,vs in class_occ.items(): model.Add(sum(vs)+len(event_cells[("c",key[0],key[1],key[2])])<=1)
    for key,vs in teacher_occ.items(): model.Add(sum(vs)+len(event_cells[("t",key[0],key[1],key[2])])<=1)
    for t,d,p in blocks:
        for (oid,occ,dd,start),v in x.items():
            if t in by[oid]["teachers"] and dd==d and start<=p<start+by[oid]["duration"]: model.Add(v==0)
    # Soft preferences: priority-weighted costs. Avoid/Prefer may be specified for a day and optionally a period.
    objective=[]
    priority_weight={"Baixa":2,"Média":6,"Alta":15}
    for _,r in prefs.fillna("").iterrows():
        teacher=str(r.get("professor","")).strip(); day=str(r.get("dia","")).strip()
        if not teacher or day not in DIAS_MAP or DIAS_MAP[day]>=n_days: continue
        period=pd.to_numeric(r.get("aula",""),errors="coerce")
        mode=str(r.get("preferencia","Preferir")); w=priority_weight.get(str(r.get("prioridade","Média")),6)
        for (oid,occ,d,p),v in x.items():
            off=by[oid]
            if teacher not in off["teachers"] or d!=DIAS_MAP[day]: continue
            matches=pd.isna(period) or any(p+k==int(period)-1 for k in range(off["duration"]))
            if matches:
                objective.append((-w if mode=="Preferir" else w)*v)
    # Soft balancing, windows, and repeated same-subject days.
    for typ,names,occmap in ([("c",{c for o in by.values() for c in o["classes"]},class_occ),("t",{t for o in by.values() for t in o["teachers"]},teacher_occ)] if (weights["janelas"] or weights["equilibrio"]) else []):
        for name in names:
            daily=[]
            for d in range(n_days):
                cells=[]
                for p in range(n_periods):
                    raw=occmap.get((name,d,p),[]); b=model.NewBoolVar(f"occ_{typ}_{abs(hash(name))%10000}_{d}_{p}")
                    if raw: model.Add(sum(raw)>=1).OnlyEnforceIf(b); model.Add(sum(raw)==0).OnlyEnforceIf(b.Not())
                    else: model.Add(b==0)
                    cells.append(b)
                daily.append(sum(cells))
                for p in range(1,n_periods-1):
                    before=model.NewBoolVar("bef"); after=model.NewBoolVar("aft"); gap=model.NewBoolVar("gap")
                    model.AddMaxEquality(before,cells[:p]); model.AddMaxEquality(after,cells[p+1:])
                    model.Add(gap<=before); model.Add(gap<=after); model.Add(gap+cells[p]<=1); model.Add(gap>=before+after-cells[p]-1)
                    objective.append(weights["janelas"]*gap)
            weekly=sum(daily)
            for d in range(n_days):
                dev=model.NewIntVar(0,n_days*n_periods*n_days,"balance")
                model.AddAbsEquality(dev,daily[d]*n_days-weekly)
                objective.append(weights["equilibrio"]*dev)
    if weights["repeticao"]:
        for oid,off in by.items():
            for d in range(n_days):
                dayvars=[]
                for occ in range(off["count"]):
                    starts=[v for (o,oi,dd,p),v in x.items() if o==oid and oi==occ and dd==d]
                    if starts:
                        b=model.NewBoolVar("lesson_day"); model.Add(sum(starts)==b); dayvars.append(b)
                for i in range(len(dayvars)):
                    for j in range(i+1,len(dayvars)):
                        both=model.NewBoolVar("repeat"); model.AddBoolAnd([dayvars[i],dayvars[j]]).OnlyEnforceIf(both)
                        model.AddBoolOr([dayvars[i].Not(),dayvars[j].Not()]).OnlyEnforceIf(both.Not())
                        objective.append(weights["repeticao"]*both)
    w_stab=int(weights.get("estabilidade",0)) if reference else 0
    if w_stab:   # mantém as demais aulas onde já estão (usado pelo editor visual)
        for (oid,occ),pos in reference.items():
            v=x.get((oid,occ,pos[0],pos[1]))
            if v is not None: objective.append(-w_stab*v)
    model.Minimize(sum(objective) if objective else 0)
    solver=cp_model.CpSolver(); solver.parameters.max_time_in_seconds=float(time_limit); solver.parameters.num_search_workers=8
    progress=_Progress(); status=solver.Solve(model,progress); solver.progress=progress.history
    if status not in (cp_model.OPTIMAL,cp_model.FEASIBLE): return None,solver.StatusName(status),solver
    rows=[]
    for (oid,occ,d,p),v in x.items():
        if solver.Value(v):
            o=by[oid]; rows.append({"oferta":oid,"ocorrencia":occ+1,"dia":DIAS[d],"dia_idx":d,"inicio":p+1,"duracao":o["duration"],"turmas":o["classes"],"disciplina":o["subject"],"professores":o["teachers"]})
    # "turmas" e "professores" são listas; pandas não consegue ordená-las
    # diretamente (TypeError: unhashable type: 'list'). Use colunas escalares.
    result = pd.DataFrame(rows)
    result["_turmas_ord"] = result["turmas"].apply(lambda xs: ", ".join(xs))
    result = result.sort_values(
        ["dia_idx", "inicio", "_turmas_ord", "disciplina", "oferta"]
    ).drop(columns=["_turmas_ord"]).reset_index(drop=True)
    return result, solver.StatusName(status), solver


def registry_to_offers(vinculos):
    """Converte vínculos em ofertas; docentes da mesma turma/disciplina viram co-docentes."""
    cols = ["turma", "professor", "disciplina", "aulas_semana", "duracao"]
    if vinculos is None or vinculos.empty:
        return pd.DataFrame(columns=OFFER_COLS)
    data = vinculos.copy().fillna("")
    for col in ["turma", "professor", "disciplina"]:
        data[col] = data[col].astype(str).str.strip()
    data = data[(data.turma != "") & (data.professor != "") & (data.disciplina != "")]
    if data.empty:
        return pd.DataFrame(columns=OFFER_COLS)
    data["aulas_semana"] = pd.to_numeric(data["aulas_semana"], errors="raise").astype(int)
    data["duracao"] = pd.to_numeric(data["duracao"], errors="raise").astype(int)
    if (data.aulas_semana < 1).any() or (~data.duracao.isin([1, 2, 3])).any():
        raise ValueError("Cada vínculo deve ter ao menos uma aula semanal e duração de 1, 2 ou 3 períodos.")
    duplicated = data.duplicated(["turma", "professor", "disciplina"], keep=False)
    if duplicated.any():
        repeated = data.loc[duplicated, ["turma", "professor", "disciplina"]].drop_duplicates()
        detail = "; ".join(f"{r.turma} / {r.disciplina} / {r.professor}" for r in repeated.itertuples(index=False))
        raise ValueError("Há vínculos repetidos para o mesmo professor e disciplina: " + detail)

    rows = []
    # A turma e a disciplina definem a aula. Todos os professores associados
    # a essa combinação serão alocados juntos, no mesmo horário.
    for i, ((turma, disciplina), group) in enumerate(
        data.groupby(["turma", "disciplina"], sort=True), start=1
    ):
        if group.aulas_semana.nunique() != 1 or group.duracao.nunique() != 1:
            docentes = ", ".join(sorted(group.professor.unique()))
            raise ValueError(
                f"Co-docência inconsistente em {turma} / {disciplina} ({docentes}): "
                "todos os professores da mesma aula precisam ter a mesma quantidade semanal e duração."
            )
        teachers = ", ".join(sorted(set(group.professor)))
        count = int(group.aulas_semana.iloc[0])
        duration = int(group.duracao.iloc[0])
        stable_id = "CAD-" + hashlib.sha1(f"{turma}|{disciplina}".encode("utf-8")).hexdigest()[:10].upper()
        rows.append([stable_id, turma, disciplina, teachers, count, duration, "", ""])
    return pd.DataFrame(rows, columns=OFFER_COLS)


def merge_registry_offers(manual_offers, vinculos):
    """Combina ofertas manuais e automáticas sem duplicar as geradas pelo cadastro."""
    manual = manual_offers.copy().fillna("")
    if "id" not in manual:
        manual["id"] = ""
    manual = manual[~manual.id.astype(str).str.startswith("CAD-")]
    generated = registry_to_offers(vinculos)
    return pd.concat([manual.reindex(columns=OFFER_COLS), generated], ignore_index=True)



def apply_sync_groups(offers, sync_table):
    """Aplica grupos de sincronização editados separadamente às ofertas manuais e automáticas."""
    data = offers.copy().fillna("").reindex(columns=OFFER_COLS)
    if sync_table is None or sync_table.empty:
        return data
    mapping = {
        str(row.id).strip(): str(row.grupo).strip()
        for row in sync_table.fillna("").itertuples(index=False)
        if str(row.id).strip()
    }
    for idx, offer_id in data.id.astype(str).items():
        if offer_id in mapping:
            data.at[idx, "sincronizar"] = mapping[offer_id]
    return collapse_sync_groups(data)


def collapse_sync_groups(data):
    """Funde as ofertas de um mesmo grupo de sincronização em UMA oferta.

    Motivo: no modelo CP-SAT, duas ofertas "sincronizadas" que compartilham o
    mesmo professor (ex.: Yoga em 7A, 7B e 7C) são impossíveis, porque o
    professor seria contado 3 vezes na mesma célula. Fundindo, o grupo passa a
    ocupar todas as turmas e todos os professores ao mesmo tempo, uma única vez.
    """
    data = data.copy().fillna("")
    groups = {}
    rows = []
    for _, r in data.iterrows():
        g = str(r.sincronizar).strip()
        if not g:
            rows.append(r.to_dict())
            continue
        if g not in groups:
            d = r.to_dict()
            d["_turmas"] = list(csv_list(r.turmas)); d["_profs"] = list(csv_list(r.professores)); d["_disc"] = [str(r.disciplina).strip()]
            groups[g] = d; rows.append(d)
        else:
            d = groups[g]
            if int(r.aulas_semana) != int(d["aulas_semana"]) or int(r.duracao) != int(d["duracao"]):
                raise ValueError(f"Grupo simultâneo '{g}': ofertas precisam ter a mesma quantidade semanal e duração.")
            for t in csv_list(r.turmas):
                if t not in d["_turmas"]: d["_turmas"].append(t)
            for t in csv_list(r.professores):
                if t not in d["_profs"]: d["_profs"].append(t)
            if str(r.disciplina).strip() not in d["_disc"]: d["_disc"].append(str(r.disciplina).strip())
    for d in groups.values():
        d["turmas"] = ", ".join(sorted(d.pop("_turmas")))
        d["professores"] = ", ".join(sorted(d.pop("_profs")))
        d["disciplina"] = " / ".join(d.pop("_disc"))
        d["sincronizar"] = ""
    return pd.DataFrame(rows, columns=OFFER_COLS)


STATE_FILE = Path(__file__).resolve().parent / "dados_horarios.json"
PDF_KEYS = ["pdf_school", "pdf_subtitle", "pdf_color", "pdf_times_on", "pdf_start", "pdf_lesson", "pdf_break_after", "pdf_break_min"]
PDF_DEFAULTS = {"pdf_school": "", "pdf_subtitle": "", "pdf_color": True, "pdf_times_on": False, "pdf_start": "07:30", "pdf_lesson": 50, "pdf_break_after": 3, "pdf_break_min": 20}


STATE_TABLES = {
    "ofertas": OFFER_COLS,
    "bloqueios": BLOCK_COLS,
    "sincronizacoes": SYNC_COLS,
    "preferencias": PREF_COLS,
    "atividades": EVENT_COLS,
    "vinculos": ["turma", "professor", "disciplina", "aulas_semana", "duracao"],
}


def load_persisted_state():
    if not STATE_FILE.exists():
        return {}
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        restored = {}
        for key, cols in STATE_TABLES.items():
            restored[key] = pd.DataFrame(saved.get("tables", {}).get(key, []), columns=cols).fillna("")
        catalogs = saved.get("catalogos", {})
        restored["catalogos"] = {
            name: pd.DataFrame(catalogs.get(name, []), columns=CATALOG_COLS).fillna("")
            for name in ["professores", "turmas", "disciplinas"]
        }
        result = saved.get("resultado")
        restored["resultado"] = pd.DataFrame(result) if result is not None else None
        restored["travas"] = {
            (str(x[0]), int(x[1])): (int(x[2]), int(x[3]))
            for x in saved.get("travas", [])
        }
        restored["manual_changes"] = int(saved.get("manual_changes", 0))
        restored["status"] = saved.get("status")
        restored["config"] = saved.get("config", {})
        return restored
    except Exception as exc:
        st.warning(f"O arquivo de dados salvos não pôde ser lido ({exc}). Os dados desta sessão não serão sobrescritos até novo salvamento.")
        return {}


def save_persisted_state():
    payload = {
        "version": 3,
        "tables": {key: json.loads(st.session_state[key].fillna("").to_json(orient="records", force_ascii=False)) for key in STATE_TABLES},
        "catalogos": {name: json.loads(df.fillna("").to_json(orient="records", force_ascii=False)) for name, df in st.session_state.catalogos.items()},
        "resultado": json.loads(st.session_state.resultado.fillna("").to_json(orient="records", force_ascii=False)) if st.session_state.resultado is not None else None,
        "travas": [[oid, occ, pos[0], pos[1]] for (oid, occ), pos in st.session_state.travas.items()],
        "manual_changes": st.session_state.manual_changes,
        "status": st.session_state.get("status"),
        "config": {
            "n_days": int(st.session_state.get("cfg_n_days", 5)),
            "n_periods": int(st.session_state.get("cfg_n_periods", 6)),
            "weights": {k: int(st.session_state.get("cfg_weight_" + k, v)) for k, v in {"janelas": 8, "equilibrio": 2, "repeticao": 3}.items()},
            "time_limit": int(st.session_state.get("cfg_time_limit", 120)),
            "day_default": st.session_state.get("cfg_day_default", "Sem limite"),
            "day_rules": st.session_state.get("cfg_day_rules", {}),
            "pdf": {k: st.session_state.get(k) for k in PDF_KEYS if k in st.session_state},
        },
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="horarios_", suffix=".json", dir=str(STATE_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, STATE_FILE)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


LIMIT_LABELS={"Sem limite":None,"1 por dia":1,"2 por dia":2,"1":1,"2":2,"3":3}


def current_day_limits(data):
    default=LIMIT_LABELS.get(st.session_state.get("cfg_day_default","Sem limite"))
    rules={x:LIMIT_LABELS.get(v) for x,v in st.session_state.get("cfg_day_rules",{}).items()}
    return compute_day_limits(data,default,rules,int(st.session_state.cfg_n_days))


def render_diagnostico(d):
    st.markdown("### 🩺 Diagnóstico: o que está impedindo o horário")
    if d["estado"]=="viavel":
        st.info("As regras obrigatórias não se contradizem. O solver provavelmente esgotou o tempo: aumente o 'Tempo máximo' na barra lateral e gere de novo.")
    elif d["estado"]=="desconhecido":
        st.warning("O diagnóstico não conseguiu concluir no tempo disponível. Use as contas rápidas abaixo como pista.")
    else:
        st.error(f"Foram encontrados {len(d['conflitos'])} conflito(s) independente(s) em {d['tempo']} s."+(" A lista pode estar incompleta: o tempo do diagnóstico acabou." if d["parcial"] else ""))
    if d["estaticos"]:
        st.markdown("**Contas rápidas**")
        for m in d["estaticos"]: st.write("• "+m)
    if d["categorias"]:
        st.markdown("**Quais tipos de regra mais aparecem nos conflitos**")
        st.dataframe(pd.DataFrame([{"Tipo de regra":r["Categoria"],"Conflitos em que aparece":f"{r['Conflitos']} de {r['de']}"} for r in d["categorias"]]),width="stretch",hide_index=True)
    if d["ranking"]:
        st.markdown("**Regras que mais causaram problema** (mais conflitos primeiro)")
        st.dataframe(pd.DataFrame(d["ranking"][:15]).rename(columns={"Conflitos":"Nº de conflitos"}),width="stretch",hide_index=True)
    for i,c in enumerate(d["conflitos"],1):
        with st.expander(f"Conflito {i}: "+(c["texto"][:95]+("…" if len(c["texto"])>95 else "")),expanded=(i==1)):
            st.write(c["texto"]); st.write("**Regras envolvidas:**")
            for r in c["regras"]: st.write("• "+r)
    if d["relaxadas"]:
        st.caption(f"Caminho de correção: ao ajustar uma regra de cada conflito ({len(d['relaxadas'])} no total), o horário passa a ser possível. Sugestões: "+"; ".join(d["relaxadas"]))
    st.download_button("⬇️ Baixar diagnóstico (.txt)",dg.to_text(d).encode("utf-8"),"diagnostico_horario.txt","text/plain",key="dl_diag")


DISC_ICONS=["🟦","🟩","🟨","🟧","🟥","🟪","🟫","⬛","⬜"]
STATUS_ICON={"atual":"🔶","direto":"🟢","troca":"🔁","reorg":"🟡","?":"⚪","nao":"🔴"}


def _ed_act(*a):
    st.session_state["ed_act"]=a


def _short(s,n):
    s=str(s); return s if len(s)<=n else s[:n-1]+"…"


def render_editor(n_days,n_periods,weights):
    st.subheader("Editor visual de horários")
    result=st.session_state.resultado
    if result is None:
        st.info("Gere um horário primeiro."); return
    try:
        eff_offers=apply_sync_groups(merge_registry_offers(st.session_state.ofertas, st.session_state.vinculos), st.session_state.sincronizacoes)
        ed_data,ed_blocks=normalize(eff_offers,st.session_state.bloqueios,n_days,n_periods)
        ed_limits,_=current_day_limits(ed_data)
        ctx=eh.Contexto(ed_data,ed_blocks,st.session_state.atividades,n_days,n_periods,ed_limits)
        pos=eh.positions(result)
        if set(k[0] for k in pos)!=set(ctx.offers) or any((o,k) not in pos for o,off in ctx.offers.items() for k in range(off["count"])):
            st.warning("O horário atual não corresponde aos cadastros (ofertas, aulas por semana ou grade mudaram). Gere o horário de novo na aba Gerar / reotimizar."); return
    except Exception as e:
        st.error(f"Não foi possível preparar o editor: {e}"); return

    # ---------- escolha do que visualizar ----------
    c1,c2=st.columns([1.3,2])
    kind_label=c1.radio("Ver por",["Turma","Professor"],horizontal=True,key="ed_kind")
    kind="turma" if kind_label=="Turma" else "professor"
    names=sorted({n for k in pos for n in (ctx.offers[k[0]]["classes"] if kind=="turma" else ctx.offers[k[0]]["teachers"])})
    name=c2.selectbox(kind_label,names,key=f"ed_name_{kind}")
    view=(kind,name)
    if st.session_state.get("ed_view")!=view:
        st.session_state.ed_view=view; st.session_state.ed_sel=None
    with st.expander("⚙️ Opções do editor"):
        o1,o2=st.columns(2)
        o1.checkbox("Travar automaticamente as aulas que eu mover",value=True,key="ed_lock",help="Aulas movidas por você viram travas: ficam onde você colocou nas próximas gerações e reorganizações.")
        o2.slider("Tempo máximo para reorganizar as outras aulas (s)",3,120,15,key="ed_reorg_time")

    notice=st.session_state.pop("ed_notice",None)
    act=st.session_state.pop("ed_act",None)
    def commit(new_pos,lock_keys,text):
        h=st.session_state.setdefault("ed_hist",[]); h.append((st.session_state.resultado.copy(),dict(st.session_state.travas))); del h[:-20]
        st.session_state.resultado=eh.apply_positions(st.session_state.resultado,new_pos)
        if st.session_state.get("ed_lock",True):
            for k in lock_keys: st.session_state.travas[k]=new_pos[k]
        st.session_state.manual_changes+=1; st.session_state.ed_sel=None; st.session_state.diagnostico=None
        return ("success",text)
    def jan(res,who):
        r=ph.load_report(res,st.session_state.atividades,days,n_periods); r=r[(r.Tipo==kind_label)&(r.Nome==who)]
        return int(r.Janelas.iloc[0]) if len(r) else 0
    own=eh.owners(ctx,pos,kind,name)
    sel=st.session_state.get("ed_sel")
    if sel is not None and sel not in pos: sel=st.session_state.ed_sel=None
    ckey=lambda: hashlib.md5(repr((sorted(pos.items()),sel,sorted(st.session_state.travas.items()))).encode()).hexdigest()
    if act:
        if act[0]=="cancel": st.session_state.ed_sel=None; sel=None
        elif act[0]=="undo" and st.session_state.get("ed_hist"):
            r_,t_=st.session_state.ed_hist.pop(); st.session_state.resultado=r_; st.session_state.travas=t_; st.session_state.ed_sel=None
            result=r_; pos=eh.positions(result); own=eh.owners(ctx,pos,kind,name); sel=None; notice=("info","Última alteração desfeita.")
        elif act[0]=="unlock":
            st.session_state.travas.pop((act[1],act[2]),None); notice=("info","Trava removida.")
        elif act[0]=="unlock_all":
            st.session_state.travas={}; notice=("info","Todas as travas foram removidas.")
        elif act[0]=="improve":
            before=jan(result,name)
            with st.spinner("Melhorando o horário e mantendo suas edições..."):
                try:
                    res2,status2,sv=build_solve(ed_data,ed_blocks,st.session_state.atividades,st.session_state.preferencias,st.session_state.travas,n_days,n_periods,min(60,int(time_limit)),{**weights,"estabilidade":10},ed_limits,reference=pos)
                except Exception as e: res2,status2=None,str(e)
            if res2 is None: notice=("error",f"Não foi possível melhorar: {status2}.")
            else:
                new=eh.positions(res2); moved=sum(1 for k in pos if new[k]!=pos[k])
                notice=commit(new,[],f"Horário reotimizado mantendo suas travas: {moved} aula(s) mudaram de lugar. Janelas de {name}: {before} → {jan(st.session_state.resultado,name)}.")
                result=st.session_state.resultado; pos=eh.positions(result); own=eh.owners(ctx,pos,kind,name)
        elif act[0]=="cell":
            d,p=act[1],act[2]
            if sel is None:
                ks=list(dict.fromkeys(own.get((d,p),[])))
                if ks: st.session_state.ed_sel=sel=ks[0]
            elif sel in own.get((d,p),[]): st.session_state.ed_sel=None; sel=None
            else:
                g=eh.grid_status(ctx,pos,sel,kind,name); s_,why=g[(d,p)]
                cached=st.session_state.get("ed_cache",{}).get(ckey(),{})
                if s_=="?" and cached.get((d,p))=="nao": s_,why="nao","Mesmo reorganizando as outras aulas, não existe horário válido com a aula aqui. Motivo imediato: "+why
                before=jan(result,name)
                if s_=="nao": notice=("warning","Não é possível: "+why)
                elif s_=="direto":
                    new=dict(pos); new[sel]=(d,p)
                    notice=commit(new,[sel],f"Aula movida para {DIAS[d]}, {p+1}ª aula. Janelas de {name}: {before} → {jan(eh.apply_positions(result,new),name)}.")
                elif s_=="troca":
                    other=eh.swap_partner(ctx,pos,sel,kind,name,d,p); new=dict(pos); new[other]=pos[sel]; new[sel]=(d,p)
                    notice=commit(new,[sel,other],f"Troca feita com {ctx.nome(other)}. Janelas de {name}: {before} → {jan(eh.apply_positions(result,new),name)}.")
                else:
                    locked2=dict(st.session_state.travas); locked2[sel]=(d,p)
                    with st.spinner("Reorganizando as outras aulas para abrir espaço..."):
                        try:
                            res2,status2,sv=build_solve(ed_data,ed_blocks,st.session_state.atividades,st.session_state.preferencias,locked2,n_days,n_periods,st.session_state.get("ed_reorg_time",15),{"janelas":0,"equilibrio":0,"repeticao":0,"estabilidade":40},ed_limits,reference=pos)
                        except Exception as e: res2,status2=None,str(e)
                    if res2 is None:
                        notice=("error",f"Não encontrei como encaixar a aula aqui ({status2}). Veja o motivo abaixo.")
                        try: st.session_state.ed_diag=dg.diagnosticar(ed_data,ed_blocks,st.session_state.atividades,locked2,n_days,n_periods,tempo_total=15,limits=ed_limits)
                        except Exception: st.session_state.ed_diag=None
                    else:
                        new=eh.positions(res2); moved=[k for k in pos if new[k]!=pos[k] and k!=sel]
                        txt=f"Aula movida para {DIAS[d]}, {p+1}ª aula. Para abrir espaço, {len(moved)} outra(s) aula(s) mudaram: "+"; ".join(f"{ctx.nome(k)} → {DIAS[new[k][0]]}, {new[k][1]+1}ª" for k in moved[:6])+("…" if len(moved)>6 else "")+"."
                        notice=commit(new,[sel],txt+f" Janelas de {name}: {before} → {jan(eh.apply_positions(result,new),name)}.")
                        st.session_state.ed_diag=None
        if act[0] in ("cell","improve","undo","unlock","unlock_all"):
            st.session_state.ed_notice=notice; st.rerun()   # atualiza também os contadores das outras abas

    # ---------- painel de controle ----------
    viol=eh.violations(ctx,pos)
    rep=ph.load_report(result,st.session_state.atividades,days,n_periods); row=rep[(rep.Tipo==kind_label)&(rep.Nome==name)]
    m1,m2,m3,m4=st.columns(4)
    m1.metric("Conflitos no horário",len(viol),help="Regras obrigatórias violadas no horário inteiro. O editor só permite mover para onde não cria conflitos.")
    m2.metric("Aulas por semana",int(row["Aulas/semana"].iloc[0]) if len(row) else 0)
    m3.metric("Janelas",int(row.Janelas.iloc[0]) if len(row) else 0,help="Períodos vazios entre aulas no mesmo dia.")
    m4.metric("Travas manuais",len(st.session_state.travas))
    b1,b2,b3,b4=st.columns(4)
    hist=st.session_state.get("ed_hist",[])
    b1.button(f"↩️ Desfazer ({len(hist)})",disabled=not hist,on_click=_ed_act,args=("undo",),width="stretch")
    b2.button("✖️ Cancelar seleção",disabled=sel is None,on_click=_ed_act,args=("cancel",),width="stretch")
    b3.button("🔄 Melhorar mantendo minhas edições",on_click=_ed_act,args=("improve",),width="stretch",help="Roda o otimizador com seus pesos, respeitando as travas e mexendo o mínimo possível nas outras aulas.")
    b4.button("🧹 Remover todas as travas",disabled=not st.session_state.travas,on_click=_ed_act,args=("unlock_all",),width="stretch")
    if notice: getattr(st,notice[0])(notice[1])

    # ---------- busca de posições para a aula selecionada ----------
    stat={}
    if sel is not None:
        o=ctx.offers[sel[0]]
        g=eh.grid_status(ctx,pos,sel,kind,name)
        cands=[dp for dp,(s_,_) in g.items() if s_=="?"]
        cache=st.session_state.setdefault("ed_cache",{})
        if len(cache)>40: cache.clear()
        k_=ckey()
        if cands and k_ not in cache:
            bar=st.progress(0.0,"Buscando posições possíveis reorganizando as outras aulas...")
            try: cache[k_]=eh.buscar_reorganizacoes(ctx,ed_data,ed_blocks,st.session_state.atividades,st.session_state.travas,pos,sel,cands,2.0,20.0,progresso=lambda f,t_: bar.progress(min(f,1.0),t_))
            except Exception: cache[k_]={}
            bar.empty()
        cached=cache.get(k_,{})
        for dp,(s_,why) in g.items():
            if s_=="?":
                c_=cached.get(dp)
                if c_=="reorg": s_,why="reorg","Possível reorganizando outras aulas (o app move o mínimo necessário). Motivo de não ser direto: "+why
                elif c_=="nao": s_,why="nao","Impossível, mesmo reorganizando as outras aulas. Motivo imediato: "+why
                else: s_,why="?","Não deu tempo de testar. Clique para tentar. Motivo de não ser direto: "+why
            stat[dp]=(s_,why)
        cnt=Counter(s_ for s_,_ in stat.values())
        st.info(f"**Aula selecionada:** {ctx.nome(sel)} · {', '.join(o['teachers'])} · {DIAS[pos[sel][0]]}, {pos[sel][1]+1}ª aula. Clique no destino:  🟢 livre ({cnt['direto']})  ·  🔁 troca ({cnt['troca']})  ·  🟡 reorganiza outras aulas ({cnt['reorg']})  ·  🔴 impossível ({cnt['nao']})"+(f"  ·  ⚪ não testado ({cnt['?']})" if cnt['?'] else "")+". Passe o mouse sobre a célula para ver detalhes.")
    else:
        st.caption("Clique em uma aula para escolher o que mover. Depois o editor mostra, em cada horário, se dá para mover direto, trocar ou reorganizar as outras aulas.")

    # ---------- grade ----------
    subjects=sorted({o["subject"] for o in ctx.offers.values()}); icon_of={s_:DISC_ICONS[i%len(DISC_ICONS)] for i,s_ in enumerate(subjects)}
    widths=[0.7]+[2]*n_days
    head=st.columns(widths); head[0].markdown("**Aula**")
    for d in range(n_days): head[d+1].markdown(f"**{days[d]}**")
    for p in range(n_periods):
        cols=st.columns(widths); cols[0].markdown(f"**{p+1}ª**")
        for d in range(n_days):
            keys=list(dict.fromkeys(own.get((d,p),[]))); fixed_txt=eh.view_fixed(ctx,kind,name,d,p)
            s_,why=stat.get((d,p),("",""))
            if len(keys)>1: label="⚠️ "+" + ".join(_short(ctx.offers[k[0]]["subject"],10) for k in keys[:2]); tip="Conflito: "+" e ".join(ctx.nome(k) for k in keys)
            elif keys:
                k=keys[0]; o=ctx.offers[k[0]]; who=", ".join(o["teachers"] if kind=="turma" else o["classes"])
                label=("" if pos[k]==(d,p) else "↳ ")+f"{icon_of[o['subject']]} {_short(o['subject'],15)} · {_short(who,13)}"
                tip=f"{ctx.nome(k)} · {', '.join(o['teachers'])}"+(f" · aula {k[1]+1}"+(" · 🔒 travada" if k in st.session_state.travas else ""))
            elif fixed_txt: label,tip=fixed_txt,"Reservado: não pode receber aulas."
            else: label,tip="—","Horário livre."
            if s_ and s_!="atual": label=f"{STATUS_ICON[s_]} {label}"; tip=f"{why}\n\n{tip}" if keys else why
            if s_=="atual": label="🔶 "+label
            is_sel=sel is not None and sel in keys
            cols[d+1].button(label,key=f"edc_{kind}_{name}_{d}_{p}",help=tip,type="primary" if is_sel else "secondary",disabled=bool(fixed_txt and not keys),on_click=_ed_act,args=("cell",d,p),width="stretch")

    # ---------- detalhes ----------
    if st.session_state.get("ed_diag"):
        with st.expander("Por que não foi possível encaixar a aula?",expanded=True): render_diagnostico(st.session_state.ed_diag)
    with st.expander(f"Conflitos do horário ({len(viol)})",expanded=bool(viol)):
        if viol:
            for v in viol[:50]: st.write("• "+v)
        else: st.write("Nenhum conflito: todas as regras obrigatórias estão respeitadas. ✅")
    with st.expander(f"Travas manuais ({len(st.session_state.travas)})"):
        if st.session_state.travas:
            for (oid,occ),(d,p) in list(st.session_state.travas.items())[:60]:
                t1,t2=st.columns([5,1]); nm=ctx.nome((oid,occ)) if oid in ctx.offers else oid
                t1.write(f"🔒 {nm} · aula {occ+1} → {DIAS[d]}, {p+1}ª aula")
                t2.button("Remover",key=f"unl_{oid}_{occ}",on_click=_ed_act,args=("unlock",oid,occ))
        else: st.write("Nenhuma trava. Aulas que você mover ficam travadas automaticamente (opção acima).")



def excel_template():
    b=io.BytesIO()
    with pd.ExcelWriter(b,engine="openpyxl") as w:
        blank_offerings().to_excel(w,sheet_name="ofertas",index=False)
        frame([],BLOCK_COLS).to_excel(w,sheet_name="bloqueios",index=False)
        frame([],PREF_COLS).to_excel(w,sheet_name="preferencias",index=False)
        frame([],EVENT_COLS).to_excel(w,sheet_name="atividades_fixas",index=False)
        frame([],CATALOG_COLS).to_excel(w,sheet_name="professores",index=False)
        frame([],CATALOG_COLS).to_excel(w,sheet_name="turmas",index=False)
        frame([],CATALOG_COLS).to_excel(w,sheet_name="disciplinas",index=False)
    return b.getvalue()

# Session state: restaura automaticamente os dados locais ao reabrir o aplicativo.
_persisted = load_persisted_state()
_defaults = {
    "ofertas": blank_offerings(),
    "bloqueios": frame([], BLOCK_COLS),
    "sincronizacoes": frame([], SYNC_COLS),
    "preferencias": frame([], PREF_COLS),
    "atividades": frame([], EVENT_COLS),
    "catalogos": {
        "professores": frame([[x] for x in ["Ana", "Bruno", "Carlos"]], CATALOG_COLS),
        "turmas": frame([[x] for x in ["1A", "1B"]], CATALOG_COLS),
        "disciplinas": frame([[x] for x in ["Matemática", "Física", "Educação Física"]], CATALOG_COLS),
    },
    "vinculos": frame([], ["turma", "professor", "disciplina", "aulas_semana", "duracao"]),
    "resultado": None, "travas": {}, "manual_changes": 0, "status": None,
}
for key, default in _defaults.items():
    if key not in st.session_state:
        st.session_state[key] = _persisted.get(key, default)
_saved_config = _persisted.get("config", {})
st.session_state.setdefault("cfg_n_days", int(_saved_config.get("n_days", 5)))
st.session_state.setdefault("cfg_n_periods", int(_saved_config.get("n_periods", 6)))
st.session_state.setdefault("cfg_weight_janelas", int(_saved_config.get("weights", {}).get("janelas", 8)))
st.session_state.setdefault("cfg_weight_equilibrio", int(_saved_config.get("weights", {}).get("equilibrio", 2)))
st.session_state.setdefault("cfg_weight_repeticao", int(_saved_config.get("weights", {}).get("repeticao", 3)))
st.session_state.setdefault("cfg_time_limit", int(_saved_config.get("time_limit", 120)))
st.session_state.setdefault("cfg_day_default", _saved_config.get("day_default", "Sem limite"))
st.session_state.setdefault("cfg_day_rules", dict(_saved_config.get("day_rules", {})))
for _k, _v in PDF_DEFAULTS.items():
    st.session_state.setdefault(_k, _saved_config.get("pdf", {}).get(_k, _v))

st.title("🗓️ Gerador de Horários Escolares")
st.caption("Programação por restrições (OR-Tools CP-SAT), edição visual, importação Excel e relatórios.")
with st.sidebar:
    st.header("Grade semanal")
    n_days=st.selectbox("Dias letivos",[5,6],key="cfg_n_days"); days=DIAS[:n_days]
    n_periods=st.number_input("Aulas por dia",1,12,key="cfg_n_periods")
    st.divider(); st.subheader("Pesos da otimização")
    weights={"janelas":st.slider("Evitar janelas",0,20,key="cfg_weight_janelas"),"equilibrio":st.slider("Equilibrar carga diária",0,20,key="cfg_weight_equilibrio"),"repeticao":st.slider("Evitar repetição diária",0,20,key="cfg_weight_repeticao")}
    time_limit=st.slider("Tempo máximo (s)",5,1800,step=5,key="cfg_time_limit",help="Tempo que o solver pode usar para melhorar o horário. Ele para antes se provar que achou o melhor possível. Depois de gerar, o app mostra em que segundo ocorreu a última melhoria.")
    st.divider(); st.subheader("Regras obrigatórias")
    st.selectbox("Máx. de aulas da mesma disciplina por dia",["Sem limite","1 por dia","2 por dia"],key="cfg_day_default",help="Vale para cada turma e disciplina. Ex.: '1 por dia' impede duas aulas de Educação Física no mesmo dia. Disciplinas com mais aulas do que dias (ex.: LP com 6 aulas em 5 dias) recebem automaticamente o mínimo possível.")
    with st.expander("Exceções por disciplina"):
        _subjects=sorted({str(x).strip() for x in list(st.session_state.vinculos.disciplina)+list(st.session_state.ofertas.disciplina) if str(x).strip()})
        if _subjects:
            _rules_df=pd.DataFrame({"Disciplina":_subjects,"Máx. por dia":[st.session_state.cfg_day_rules.get(x,"Padrão") for x in _subjects]})
            _ed_rules=st.data_editor(_rules_df,hide_index=True,disabled=["Disciplina"],key="day_rules_editor",width="stretch",column_config={"Máx. por dia":st.column_config.SelectboxColumn(options=["Padrão","Sem limite","1","2","3"],required=True)})
            st.session_state.cfg_day_rules={r["Disciplina"]:r["Máx. por dia"] for _,r in _ed_rules.iterrows() if r["Máx. por dia"]!="Padrão"}
        else: st.caption("Cadastre vínculos para configurar exceções por disciplina.")
    st.caption("Salvamento automático ativado: cadastros, vínculos e configurações ficam no arquivo dados_horarios.json, na pasta do aplicativo.")

tabs=st.tabs(["Ofertas e Excel","Cadastro de turmas","Restrições e preferências","Atividades fixas","Gerar / reotimizar","Editor visual","Relatórios"])
with tabs[0]:
    st.subheader("Ofertas de aulas")
    st.caption("As ofertas cadastradas manualmente ficam nesta tabela. As ofertas criadas na aba Cadastro de turmas são incorporadas automaticamente logo abaixo e também entram no simulador.")
    st.markdown("**Ofertas manuais**")
    with st.form("manual_offers_form"):
        edited_offers = st.data_editor(st.session_state.ofertas,num_rows="dynamic",width="stretch",key="offers_editor",disabled=["sincronizar"],column_config={"duracao":st.column_config.SelectboxColumn("Duração",options=[1,2,3]),"aulas_semana":st.column_config.NumberColumn("Ocorrências/semana",min_value=1,max_value=40,step=1),"fixas":st.column_config.TextColumn("Fixas",help="Ex.: 1:Segunda-2; 2:Quarta-3")})
        save_offers = st.form_submit_button("Salvar ofertas manuais")
    if save_offers:
        st.session_state.ofertas = edited_offers.reindex(columns=OFFER_COLS).fillna("")
        st.success("Ofertas manuais salvas.")
    try:
        _auto_offers = registry_to_offers(st.session_state.vinculos)
        _effective_offers = merge_registry_offers(st.session_state.ofertas, st.session_state.vinculos)
        st.markdown("**Ofertas automáticas do cadastro**")
        if _auto_offers.empty:
            st.info("Nenhuma oferta automática ainda. Cadastre vínculos completos na aba Cadastro de turmas.")
        else:
            st.dataframe(_auto_offers, width="stretch", hide_index=True)
        st.markdown("**Sincronização manual das aulas**")
        st.caption(
            "Escolha um mesmo nome de grupo para ofertas que devem acontecer simultaneamente. "
            "Isso funciona tanto para ofertas manuais quanto para ofertas automáticas do cadastro. "
            "Deixe o campo vazio quando a oferta não precisar ser sincronizada."
        )
        sync_view = _effective_offers[["id", "turmas", "disciplina", "professores", "aulas_semana", "duracao", "sincronizar"]].copy()
        sync_view = sync_view.rename(columns={"sincronizar": "grupo"})
        # O vínculo salvo em tabela própria mantém a sincronização mesmo para ofertas automáticas.
        saved_sync = st.session_state.sincronizacoes.set_index("id")["grupo"].to_dict() if len(st.session_state.sincronizacoes) else {}
        sync_view["grupo"] = [
            saved_sync.get(str(oid), str(group) if str(group).strip() else "")
            for oid, group in zip(sync_view.id, sync_view.grupo)
        ]
        with st.form("manual_sync_form"):
            edited_sync = st.data_editor(
                sync_view, num_rows="fixed", width="stretch", key="sync_groups_editor",
                disabled=["id", "turmas", "disciplina", "professores", "aulas_semana", "duracao"],
                column_config={"grupo": st.column_config.TextColumn(
                    "Grupo de sincronização",
                    help="Ofertas com o mesmo grupo viram uma aula única: todas as turmas e professores do grupo ficam juntos, no mesmo dia e período."
                )}
            )
            save_sync = st.form_submit_button("Salvar sincronizações")
        if save_sync:
            cleaned_sync = edited_sync[["id", "grupo"]].fillna("").copy()
            cleaned_sync["id"] = cleaned_sync.id.astype(str)
            cleaned_sync["grupo"] = cleaned_sync.grupo.astype(str).str.strip()
            st.session_state.sincronizacoes = cleaned_sync.reindex(columns=SYNC_COLS)
            st.session_state.resultado = None
            st.session_state.status = None
            st.session_state.travas = {}
            st.success("Grupos de sincronização atualizados.")
        st.markdown("**Conjunto completo enviado ao simulador**")
        _effective_offers = apply_sync_groups(_effective_offers, st.session_state.sincronizacoes)
        st.dataframe(_effective_offers, width="stretch", hide_index=True)
    except Exception as exc:
        st.error(f"Não foi possível converter os vínculos em ofertas: {exc}")
    st.download_button("Baixar modelo Excel (7 abas)",excel_template(),"modelo_horarios.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    up=st.file_uploader("Importar planilha Excel",type=["xlsx"],key="excel_import")
    if up:
        try:
            book=pd.read_excel(up,sheet_name=None)
            for sheet,key,cols in [("ofertas","ofertas",OFFER_COLS),("bloqueios","bloqueios",BLOCK_COLS),("preferencias","preferencias",PREF_COLS),("atividades_fixas","atividades",EVENT_COLS)]:
                if sheet in book: st.session_state[key]=book[sheet].reindex(columns=cols,fill_value="").fillna("")
            for sheet in ["professores","turmas","disciplinas"]:
                if sheet in book: st.session_state.catalogos[sheet]=book[sheet].reindex(columns=CATALOG_COLS,fill_value="").fillna("")
            st.success("Planilha importada. Confira as quatro tabelas nas abas correspondentes."); st.rerun()
        except Exception as e: st.error(f"Falha ao importar Excel: {e}")

with tabs[1]:
    st.subheader("Cadastro de turmas e professores")
    st.write("Cadastre os nomes uma vez e, em seguida, selecione quais professores atuam em cada turma. O vínculo será convertido em ofertas para o simulador.")
    left, right = st.columns(2)
    with left:
        st.markdown("**Turmas cadastradas**")
        with st.form("add_class_form", clear_on_submit=True):
            new_class = st.text_input("Nova turma", placeholder="Ex.: 7º A")
            add_class = st.form_submit_button("Adicionar turma")
        if add_class and new_class.strip():
            current = st.session_state.catalogos["turmas"]
            names = current.nome.astype(str).str.strip().tolist()
            if new_class.strip() not in names:
                st.session_state.catalogos["turmas"] = pd.concat([current, frame([[new_class.strip()]], CATALOG_COLS)], ignore_index=True)
                st.rerun()
            else:
                st.warning("Essa turma já está cadastrada.")
        st.dataframe(st.session_state.catalogos["turmas"], width="stretch", hide_index=True)
        class_remove_options = st.session_state.catalogos["turmas"].nome.astype(str).tolist()
        if class_remove_options:
            with st.form("remove_class_form"):
                remove_class = st.selectbox("Remover turma", class_remove_options, key="remove_class_choice")
                if st.form_submit_button("Remover turma"):
                    st.session_state.catalogos["turmas"] = st.session_state.catalogos["turmas"][st.session_state.catalogos["turmas"].nome.astype(str) != remove_class].reset_index(drop=True)
                    st.session_state.vinculos = st.session_state.vinculos[st.session_state.vinculos.turma.astype(str) != remove_class].reset_index(drop=True)
                    st.rerun()
    with right:
        st.markdown("**Professores cadastrados**")
        with st.form("add_teacher_form", clear_on_submit=True):
            new_teacher = st.text_input("Novo professor", placeholder="Ex.: Ana Silva")
            add_teacher = st.form_submit_button("Adicionar professor")
        if add_teacher and new_teacher.strip():
            current = st.session_state.catalogos["professores"]
            names = current.nome.astype(str).str.strip().tolist()
            if new_teacher.strip() not in names:
                st.session_state.catalogos["professores"] = pd.concat([current, frame([[new_teacher.strip()]], CATALOG_COLS)], ignore_index=True)
                st.rerun()
            else:
                st.warning("Esse professor já está cadastrado.")
        st.dataframe(st.session_state.catalogos["professores"], width="stretch", hide_index=True)
        teacher_remove_options = st.session_state.catalogos["professores"].nome.astype(str).tolist()
        if teacher_remove_options:
            with st.form("remove_teacher_form"):
                remove_teacher = st.selectbox("Remover professor", teacher_remove_options, key="remove_teacher_choice")
                if st.form_submit_button("Remover professor"):
                    st.session_state.catalogos["professores"] = st.session_state.catalogos["professores"][st.session_state.catalogos["professores"].nome.astype(str) != remove_teacher].reset_index(drop=True)
                    st.session_state.vinculos = st.session_state.vinculos[st.session_state.vinculos.professor.astype(str) != remove_teacher].reset_index(drop=True)
                    st.rerun()

    class_names = sorted({str(x).strip() for x in st.session_state.catalogos["turmas"].nome.tolist() if str(x).strip()})
    teacher_names = sorted({str(x).strip() for x in st.session_state.catalogos["professores"].nome.tolist() if str(x).strip()})
    # Remove vínculos órfãos quando uma turma ou professor é excluído do cadastro.
    if len(st.session_state.vinculos):
        _valid_links = st.session_state.vinculos[
            st.session_state.vinculos.turma.astype(str).isin(class_names)
            & st.session_state.vinculos.professor.astype(str).isin(teacher_names)
        ].copy()
        if len(_valid_links) != len(st.session_state.vinculos):
            st.session_state.vinculos = _valid_links.reset_index(drop=True)
            st.session_state.resultado = None
            st.session_state.status = None
            st.session_state.travas = {}
    st.divider()
    st.subheader("Professores que dão aula em cada turma")
    if not class_names:
        st.info("Cadastre pelo menos uma turma para começar.")
    elif not teacher_names:
        st.info("Cadastre pelo menos um professor para ativar os vínculos.")
    else:
        selected_class = st.selectbox("Turma", class_names, key="registry_selected_class")
        existing = st.session_state.vinculos
        current_rows = existing[existing.turma.astype(str) == selected_class] if len(existing) else frame([], ["turma", "professor", "disciplina", "aulas_semana", "duracao"])
        current_teachers = sorted(set(current_rows.professor.astype(str))) if len(current_rows) else []
        active_teachers = st.multiselect("Ativar professores nesta turma", teacher_names, default=[t for t in current_teachers if t in teacher_names], key="active_teachers_" + re.sub(r"\W+", "_", selected_class))
        assignment_rows = []
        for teacher in active_teachers:
            previous = current_rows[current_rows.professor.astype(str) == teacher] if len(current_rows) else pd.DataFrame()
            if len(previous):
                assignment_rows.extend(previous[["turma", "professor", "disciplina", "aulas_semana", "duracao"]].to_dict("records"))
            else:
                assignment_rows.append({"turma": selected_class, "professor": teacher, "disciplina": "", "aulas_semana": 1, "duracao": 1})
        assignments = frame(assignment_rows, ["turma", "professor", "disciplina", "aulas_semana", "duracao"])
        st.caption("Cada linha representa um vínculo professor–disciplina. Para dois professores na mesma aula, cadastre ambos na mesma turma e disciplina, com a mesma quantidade semanal e duração. O sistema os agrupará em uma única oferta simultânea.")
        with st.form("assignments_form_" + re.sub(r"\W+", "_", selected_class)):
            edited_assignments = st.data_editor(
                assignments, num_rows="dynamic", width="stretch",
                key="assignments_" + re.sub(r"\W+", "_", selected_class),
                column_config={
                    "turma": st.column_config.TextColumn("Turma", disabled=True),
                    "professor": st.column_config.SelectboxColumn("Professor", options=active_teachers, required=True),
                    "disciplina": st.column_config.TextColumn("Disciplina", required=True),
                    "aulas_semana": st.column_config.NumberColumn("Aulas por semana", min_value=1, max_value=40, step=1, required=True),
                    "duracao": st.column_config.SelectboxColumn("Duração do bloco", options=[1, 2, 3], required=True),
                })
            save_links = st.form_submit_button("Salvar vínculos da turma", type="primary")
        if save_links:
            cleaned = edited_assignments.copy().fillna("")
            cleaned["turma"] = selected_class
            cleaned = cleaned[cleaned.professor.astype(str).str.strip().isin(active_teachers)]
            try:
                incomplete = cleaned[cleaned.disciplina.astype(str).str.strip() == ""]
                if len(incomplete):
                    raise ValueError("Preencha a disciplina de todos os professores ativados ou remova o professor da seleção.")
                missing = set(active_teachers) - set(cleaned.professor.astype(str))
                if missing:
                    raise ValueError("Inclua ao menos uma disciplina para: " + ", ".join(sorted(missing)))
                if len(cleaned):
                    cleaned["aulas_semana"] = pd.to_numeric(cleaned.aulas_semana, errors="raise").astype(int)
                    cleaned["duracao"] = pd.to_numeric(cleaned.duracao, errors="raise").astype(int)
                    if (cleaned.aulas_semana < 1).any() or (~cleaned.duracao.isin([1, 2, 3])).any():
                        raise ValueError("Aulas semanais devem ser positivas e a duração deve ser 1, 2 ou 3.")
                other_classes = st.session_state.vinculos[st.session_state.vinculos.turma.astype(str) != selected_class] if len(st.session_state.vinculos) else frame([], ["turma", "professor", "disciplina", "aulas_semana", "duracao"])
                candidate_links = pd.concat([other_classes, cleaned.reindex(columns=["turma", "professor", "disciplina", "aulas_semana", "duracao"])], ignore_index=True)
                # Valida co-docência antes de substituir os vínculos atuais.
                candidate_offers = registry_to_offers(candidate_links)
                st.session_state.vinculos = candidate_links
                st.session_state.resultado = None
                st.session_state.status = None
                st.session_state.travas = {}
                st.success(f"Vínculos salvos. {len(candidate_offers)} oferta(s) automática(s) foram atualizadas na aba Ofertas e Excel. O horário anterior foi invalidado para evitar travas conflitantes.")
            except Exception as e:
                st.error(f"Não foi possível salvar os vínculos: {e}")

    st.divider()
    st.subheader("Prévia das ofertas geradas pelo cadastro")
    try:
        generated_preview = registry_to_offers(st.session_state.vinculos)
        if generated_preview.empty:
            st.info("Ainda não há vínculos completos (com disciplina) para converter em ofertas.")
        else:
            st.dataframe(generated_preview, width="stretch", hide_index=True)
            st.caption("Todos os professores vinculados à mesma turma e disciplina aparecem juntos na coluna professores e terão exatamente o mesmo horário. Frequências ou durações diferentes são sinalizadas como inconsistência, em vez de criar aulas concorrentes.")
    except Exception as exc:
        st.error(f"Inconsistência nos vínculos: {exc}")

with tabs[2]:
    st.subheader("Indisponibilidades de professores")
    known_teachers = sorted(
        {str(x).strip() for x in st.session_state.catalogos["professores"].nome.tolist() if str(x).strip()}
        | {t for v in st.session_state.vinculos.professor.tolist() for t in csv_list(v)}
    )
    if not known_teachers:
        st.info("Cadastre professores antes de configurar as indisponibilidades.")
    else:
        selected_block_teacher = st.selectbox(
            "Professor", known_teachers, key="block_grid_teacher"
        )
        st.caption(
            "Marque as células em que o professor não pode dar aula. "
            "Cada coluna representa um período do dia; cada linha representa um dia da semana. "
            "Para retirar um bloqueio, desmarque a célula."
        )
        grid_rows = []
        current_blocks = st.session_state.bloqueios.copy().fillna("")
        for day in days:
            row = {"Dia": day}
            for period in range(1, int(n_periods) + 1):
                is_blocked = False
                for _, block in current_blocks.iterrows():
                    if str(block.get("professor", "")).strip() != selected_block_teacher:
                        continue
                    if str(block.get("dia", "")).strip() != day:
                        continue
                    whole = str(block.get("dia_inteiro", "")).strip().lower() in {"true", "1", "sim", "yes"}
                    block_period = pd.to_numeric(block.get("aula", ""), errors="coerce")
                    if whole or (not pd.isna(block_period) and int(block_period) == period):
                        is_blocked = True
                        break
                row[f"Aula {period}"] = is_blocked
            grid_rows.append(row)
        block_grid = pd.DataFrame(grid_rows)
        with st.form("block_grid_form"):
            edited_grid = st.data_editor(
                block_grid, hide_index=True, width="stretch", num_rows="fixed",
                disabled=["Dia"],
                column_config={
                    col: st.column_config.CheckboxColumn(col, help="Marque para bloquear este período.")
                    for col in block_grid.columns if col != "Dia"
                },
                key="teacher_block_grid_" + str(selected_block_teacher).replace(" ", "_")
            )
            save_grid = st.form_submit_button("Salvar grade de indisponibilidades", type="primary")
        if save_grid:
            other_blocks = current_blocks[
                current_blocks.professor.astype(str).str.strip() != selected_block_teacher
            ].copy()
            new_rows = []
            for _, row in edited_grid.iterrows():
                for period in range(1, int(n_periods) + 1):
                    if bool(row.get(f"Aula {period}", False)):
                        new_rows.append({
                            "professor": selected_block_teacher,
                            "dia": str(row["Dia"]),
                            "aula": period,
                            "dia_inteiro": False
                        })
            st.session_state.bloqueios = pd.concat(
                [other_blocks.reindex(columns=BLOCK_COLS), frame(new_rows, BLOCK_COLS)],
                ignore_index=True
            )
            st.session_state.resultado = None
            st.session_state.status = None
            st.success("Grade de indisponibilidades salva. Células desmarcadas foram desbloqueadas.")
            st.rerun()
        st.caption("A grade salva períodos individuais. Para bloquear um dia inteiro, marque todas as aulas daquela linha.")
    st.divider(); st.subheader("Preferências individuais")
    st.write("Preferências são restrições flexíveis: o solver pode contrariá-las quando necessário, pagando uma penalidade conforme a prioridade.")
    with st.form("edit_preferences_form"):
        edited_preferences = st.data_editor(st.session_state.preferencias,num_rows="dynamic",width="stretch",key="prefs_editor",column_config={"dia":st.column_config.SelectboxColumn(options=days),"aula":st.column_config.NumberColumn("Aula (opcional)",min_value=1,max_value=int(n_periods),step=1),"preferencia":st.column_config.SelectboxColumn(options=["Preferir","Evitar"]),"prioridade":st.column_config.SelectboxColumn(options=["Baixa","Média","Alta"])})
        save_preferences = st.form_submit_button("Salvar preferências")
    if save_preferences:
        st.session_state.preferencias = edited_preferences.reindex(columns=PREF_COLS).fillna("")
        st.success("Preferências atualizadas.")
    st.caption("Exemplo: Ana | Terça | 1 | Preferir | Alta. Deixe aula vazia para indicar o dia inteiro.")
    with st.expander("Cadastros auxiliares (opcionais, importados do Excel)"):
        for catalog in ["professores","turmas","disciplinas"]:
            st.write(f"**{catalog.capitalize()}**")
            st.dataframe(st.session_state.catalogos[catalog],width="stretch",hide_index=True)
with tabs[3]:
    st.subheader("Atividades fixas da escola")
    st.write("Reserve horários de prova, reunião, conselho, formação ou outras atividades. Informe as turmas e/ou professores afetados.")
    with st.form("events_form"):
        edited_events=st.data_editor(st.session_state.atividades,num_rows="dynamic",width="stretch",key="events_editor",column_config={"dia":st.column_config.SelectboxColumn(options=days),"aula":st.column_config.NumberColumn(min_value=1,max_value=int(n_periods),step=1),"duracao":st.column_config.SelectboxColumn(options=[1,2,3])})
        save_events=st.form_submit_button("Salvar atividades fixas")
    if save_events:
        st.session_state.atividades=edited_events.reindex(columns=EVENT_COLS).fillna("")
        st.success("Atividades fixas salvas.")
    st.info("Exemplo: Prova integrada | Quarta | aula 2 | duração 2 | turmas 1A, 1B | professores em branco.")
with tabs[4]:
    st.subheader("Gerar ou reotimizar")
    st.write("As travas manuais e as aulas fixadas são tratadas como restrições obrigatórias. As preferências, janelas e equilíbrio são critérios de otimização.")
    st.metric("Travas manuais preservadas",len(st.session_state.travas))
    if st.button("⚙️ Gerar / reotimizar horário",type="primary",width="stretch"):
        try:
            effective_offers = apply_sync_groups(merge_registry_offers(st.session_state.ofertas, st.session_state.vinculos), st.session_state.sincronizacoes)
            data,blocks=normalize(effective_offers,st.session_state.bloqueios,n_days,int(n_periods))
            day_limits,limit_notes=current_day_limits(data)
            if limit_notes: st.caption("ℹ️ Limite por dia ajustado automaticamente onde há mais aulas do que dias: "+"; ".join(sorted({n.split(" (")[0]+" ("+n.split(": ")[1].split(" não cabem")[0]+")" for n in limit_notes}))+".")
            issues=prediagnostics(data,blocks,n_days,int(n_periods),st.session_state.atividades,st.session_state.travas)
            if issues:
                st.warning("Verificações preliminares encontraram possíveis problemas:")
                for issue in issues: st.write("• "+issue)
            with st.spinner("O CP-SAT está resolvendo o modelo global..."):
                result,status,solver=build_solve(data,blocks,st.session_state.atividades,st.session_state.preferencias,st.session_state.travas,n_days,int(n_periods),time_limit,weights,day_limits)
            if result is None:
                st.error(f"Modelo sem solução. Status: {status}.")
                st.warning("Causas comuns: carga semanal superior à capacidade; professor ou turma com sobreposição; bloqueios incompatíveis; atividade fixa ocupando os únicos horários possíveis; sincronização incompatível; ou trava manual conflitante. Revise as verificações preliminares e remova temporariamente as restrições mais recentes para localizar a causa.")
                with st.spinner("Diagnosticando quais regras se contradizem..."):
                    st.session_state.diagnostico=dg.diagnosticar(data,blocks,st.session_state.atividades,st.session_state.travas,n_days,int(n_periods),tempo_total=max(20,min(60,int(time_limit))),limits=day_limits)
            else:
                st.session_state.resultado=result; st.session_state.status=status; st.session_state.diagnostico=None
                st.success(f"Horário válido encontrado: {status}. Pontuação de penalidades: {solver.ObjectiveValue():.0f} (quanto menor, melhor). Tempo gasto: {solver.WallTime():.0f} s de {int(time_limit)} s.")
                _hist=getattr(solver,"progress",[])
                if _hist: st.caption(f"Última melhoria aos {_hist[-1][0]:.0f} s ({len(_hist)} soluções melhores encontradas pelo caminho)."+(" Se esse instante está perto do limite, vale aumentar o tempo máximo." if status=="FEASIBLE" and _hist[-1][0]>0.7*float(time_limit) else ""))
                if status=="FEASIBLE": st.info("Solução viável encontrada, mas o limite de tempo impediu provar que é a melhor possível.")
        except Exception as e: st.error(str(e))
    if st.button("🩺 Diagnosticar conflitos (sem gerar o horário)",width="stretch",help="Procura quais regras se contradizem e quais mais aparecem nos conflitos."):
        try:
            _eff=apply_sync_groups(merge_registry_offers(st.session_state.ofertas, st.session_state.vinculos), st.session_state.sincronizacoes)
            _data,_blocks=normalize(_eff,st.session_state.bloqueios,n_days,int(n_periods))
            _limits,_=current_day_limits(_data)
            with st.spinner("Diagnosticando quais regras se contradizem..."):
                st.session_state.diagnostico=dg.diagnosticar(_data,_blocks,st.session_state.atividades,st.session_state.travas,n_days,int(n_periods),tempo_total=60,limits=_limits)
            if st.session_state.diagnostico["estado"]=="viavel" and not st.session_state.diagnostico["estaticos"]: st.success("Nenhum conflito encontrado entre as regras obrigatórias.")
        except Exception as e: st.error(str(e))
    if st.session_state.get("diagnostico"): render_diagnostico(st.session_state.diagnostico)
    if st.session_state.resultado is not None: st.dataframe(st.session_state.resultado.drop(columns=["dia_idx"]),width="stretch",hide_index=True)
with tabs[5]:
    render_editor(n_days,int(n_periods),weights)
with tabs[6]:
    st.subheader("Relatórios e exportação")
    result=st.session_state.resultado
    if result is None: st.info("Gere um horário para habilitar os relatórios.")
    else:
        events_df=st.session_state.atividades
        report=ph.load_report(result,events_df,days,int(n_periods))
        res_classes=sorted(report[report.Tipo=="Turma"].Nome.tolist()); res_teachers=sorted(report[report.Tipo=="Professor"].Nome.tolist())

        # ---------- Cargas e conferência de regras ----------
        st.markdown("#### 📊 Cargas, janelas e conferência de regras")
        c1,c2=st.columns(2)
        cap_prof=c1.number_input("Teto semanal por professor (aulas)",0,60,25,key="rep_cap_prof",help="0 = não verificar")
        target_class=c2.number_input("Carga exata por turma (aulas)",0,60,25,key="rep_target_class",help="0 = não verificar")
        over=report[(report.Tipo=="Professor")&(report["Aulas/semana"]>cap_prof)] if cap_prof else report.iloc[0:0]
        wrong=report[(report.Tipo=="Turma")&(report["Aulas/semana"]!=target_class)] if target_class else report.iloc[0:0]
        if len(over): st.error("Acima do teto: "+", ".join(f"{r.Nome} ({r['Aulas/semana']})" for _,r in over.iterrows()))
        elif cap_prof: st.success(f"Nenhum professor passa de {cap_prof} aulas semanais.")
        if len(wrong): st.warning(f"Turmas fora de {target_class} aulas: "+", ".join(f"{r.Nome} ({r['Aulas/semana']})" for _,r in wrong.iterrows()))
        elif target_class: st.success(f"Todas as turmas têm exatamente {target_class} aulas.")
        st.dataframe(report,width="stretch",hide_index=True)
        if len(events_df):
            st.write("**Atividades fixas reservadas**")
            st.dataframe(events_df,width="stretch",hide_index=True)
        report_xlsx=io.BytesIO()
        report.to_excel(report_xlsx,index=False,engine="openpyxl")
        st.download_button("Baixar relatório de cargas (Excel)",report_xlsx.getvalue(),"relatorio_cargas.xlsx")

        # ---------- Exportação ----------
        st.divider(); st.markdown("#### 📄 Exportar horários")
        with st.expander("🎨 Personalizar aparência dos PDFs e planilhas"):
            a,b=st.columns(2)
            a.text_input("Nome da escola",key="pdf_school"); b.text_input("Subtítulo (ex.: Ano letivo 2027)",key="pdf_subtitle")
            c,d=st.columns(2)
            c.toggle("Colorido por disciplina (desligue para imprimir em preto e branco)",key="pdf_color")
            d.toggle("Mostrar o horário de cada aula (ex.: 07:30–08:20)",key="pdf_times_on")
            if st.session_state.pdf_times_on:
                e,f,g,h=st.columns(4)
                e.text_input("Início da 1ª aula (HH:MM)",key="pdf_start")
                f.number_input("Duração da aula (min)",10,120,key="pdf_lesson")
                g.number_input("Intervalo depois da aula nº (0 = nenhum)",0,12,key="pdf_break_after")
                h.number_input("Duração do intervalo (min)",0,90,key="pdf_break_min")
        with st.expander("🎯 Escolher o que incluir"):
            sel_classes=st.multiselect("Turmas",res_classes,default=res_classes)
            sel_teachers=st.multiselect("Professores",res_teachers,default=res_teachers)
            i1,i2,i3=st.columns(3)
            inc_cover=i1.toggle("Capa com índice clicável",value=True)
            inc_q_cls=i2.toggle("Quadro geral das turmas (um por dia)",value=True)
            inc_q_prof=i3.toggle("Quadro geral dos professores (um por dia)",value=True)
        times=None
        if st.session_state.pdf_times_on:
            if re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d",str(st.session_state.pdf_start).strip()):
                brk=[(int(st.session_state.pdf_break_after),int(st.session_state.pdf_break_min))] if int(st.session_state.pdf_break_after)>0 and int(st.session_state.pdf_break_min)>0 else []
                times={"inicio":str(st.session_state.pdf_start).strip(),"aula":int(st.session_state.pdf_lesson),"intervalos":brk}
            else: st.warning("Horário de início inválido; use o formato HH:MM (ex.: 07:30). Os horários não serão exibidos.")
        pdf_opts={"escola":st.session_state.pdf_school,"subtitulo":st.session_state.pdf_subtitle,"colorido":bool(st.session_state.pdf_color),"horarios":times}
        quadros=tuple(k for k,on in (("turmas",inc_q_cls),("professores",inc_q_prof)) if on)
        sig=hashlib.md5(repr((result.to_json(default_handler=str),events_df.to_json(),pdf_opts,sel_classes,sel_teachers,inc_cover,quadros,days,int(n_periods))).encode()).hexdigest()
        cache=st.session_state.setdefault("export_cache",{})
        b1,b2,b3=st.columns(3)
        if b1.button("📘 Gerar PDF único com todos os horários",type="primary",width="stretch"):
            with st.spinner("Montando o PDF..."):
                cache["unico"]=(sig,ph.make_combined_pdf(result,events_df,days,int(n_periods),pdf_opts,sel_classes,sel_teachers,inc_cover,quadros))
        if b2.button("🗂️ Gerar PDFs individuais (ZIP)",width="stretch"):
            with st.spinner("Montando os PDFs..."):
                pdfs=ph.make_individual_pdfs(result,events_df,days,int(n_periods),pdf_opts,sel_classes,sel_teachers)
                z=io.BytesIO()
                with zipfile.ZipFile(z,"w",zipfile.ZIP_DEFLATED) as zf:
                    for fn,cnt in pdfs.items(): zf.writestr(fn,cnt)
                cache["zip"]=(sig,z.getvalue())
        if b3.button("📗 Gerar planilha Excel (uma aba por turma/professor)",width="stretch"):
            with st.spinner("Montando a planilha..."):
                cache["xlsx"]=(sig,ph.make_excel_grids(result,events_df,days,int(n_periods),pdf_opts,sel_classes,sel_teachers))
        files=[("unico","⬇️ Baixar PDF único (horarios_escolares.pdf)","horarios_escolares.pdf","application/pdf"),
               ("zip","⬇️ Baixar ZIP com PDFs individuais","horarios_individuais.zip","application/zip"),
               ("xlsx","⬇️ Baixar planilha Excel","horarios_grades.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
        stale=False
        for key,label,fname,mime in files:
            if key in cache:
                if cache[key][0]==sig: st.download_button(label,cache[key][1],fname,mime,key="dl_"+key)
                else: stale=True
        if stale: st.caption("⚠️ O horário ou as opções mudaram desde a última geração. Gere novamente para atualizar os arquivos.")
        st.markdown("**Baixar um único horário**")
        s1,s2,s3=st.columns([1,2,2])
        kind_pick=s1.radio("Tipo",["Turma","Professor"],horizontal=True,label_visibility="collapsed")
        names_pick=res_classes if kind_pick=="Turma" else res_teachers
        if names_pick:
            who=s2.selectbox("Nome",names_pick,label_visibility="collapsed")
            s3.download_button(f"⬇️ PDF de {who}",ph.make_resource_pdf(result,events_df,"turma" if kind_pick=="Turma" else "professor",who,days,int(n_periods),pdf_opts),f"{kind_pick}_{ph.safe_name(who)}.pdf","application/pdf",key="dl_single")

        # ---------- Quem está livre ----------
        st.divider(); st.markdown("#### 🔎 Quem está livre? (apoio para substituições)")
        f1,f2=st.columns(2)
        fd=f1.selectbox("Dia",days,key="free_day"); fp=f2.number_input("Aula",1,int(n_periods),1,key="free_period")
        catalog_teachers=[str(x).strip() for x in st.session_state.catalogos["professores"].nome.tolist() if str(x).strip()]
        free_df=ph.free_teachers(result,events_df,fd,int(fp),catalog_teachers,st.session_state.bloqueios,int(n_periods))
        n_free=int((free_df["Situação"]=="Livre").sum())
        st.caption(f"{n_free} professor(es) livre(s) em {fd}, {int(fp)}ª aula.")
        st.dataframe(free_df,width="stretch",hide_index=True)

st.divider(); st.caption("O CP-SAT usa programação por restrições e otimização combinatória; não é uma simples geração aleatória. Uma solução FEASIBLE é válida, mas não necessariamente ótima.")


# Persiste automaticamente o estado completo ao final de cada execução do Streamlit.
try:
    save_persisted_state()
except Exception as _save_exc:
    st.error(f"Não foi possível salvar os dados localmente: {_save_exc}")
