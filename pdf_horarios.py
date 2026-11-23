# -*- coding: utf-8 -*-
"""Relatórios dos horários: PDF individual, PDF único (capa + índice + marcadores),
quadros gerais por dia, planilha Excel e utilitários (cargas, janelas, quem está livre).

Este módulo não depende do Streamlit; recebe o DataFrame de resultado do solver.
"""
import io, re, math
from collections import defaultdict
from datetime import datetime
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Table, TableStyle,
                                Paragraph, Spacer, PageBreak, Flowable)

PAGE = landscape(A4)
PW, PH = PAGE
MX, MT, MB = 28, 26, 38
UW, UH = PW - 2 * MX, PH - MT - MB

# (preenchimento, texto/acento) – tons pastel com texto escuro legível
PALETTE = [
    ("#DCEBFA", "#1F4E79"), ("#FDE7D3", "#8A4B08"), ("#DFF3E4", "#1E6B3A"), ("#F6DDF0", "#7A2A66"),
    ("#FFF4C7", "#7A5E00"), ("#D9F1F4", "#116574"), ("#EADFF7", "#4B2D86"), ("#FADBD8", "#8B2C25"),
    ("#E4EDCB", "#4F6410"), ("#E3E7EC", "#37424D"), ("#FBE0E8", "#8E2447"), ("#DDE9E1", "#2F5D45"),
    ("#E8E2D4", "#5C4B22"), ("#D8E3F5", "#2A3F8F"),
]
FIXED_COLORS = ("#EDEFF2", "#5A6570")
NAVY, ACCENT, INK, MUTED, LINE, SOFT = "#1F3A5F", "#2E86AB", "#1B2733", "#6B7885", "#D3DAE1", "#F1F4F7"


# ----------------------------------------------------------------------------------
# Dados
# ----------------------------------------------------------------------------------
def _lst(v):
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return []
    return [x.strip() for x in str(v).split(",") if x.strip()]


def _num(v):
    n = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(n) else int(n)


def _event_rows(events):
    """Atividades fixas válidas -> lista de dicts."""
    out = []
    if events is None or len(events) == 0:
        return out
    for _, ev in events.fillna("").iterrows():
        name = str(ev.get("atividade", "")).strip()
        ini, dur = _num(ev.get("aula")), _num(ev.get("duracao"))
        dia = str(ev.get("dia", "")).strip()
        if not name or not ini or not dur or not dia:
            continue
        out.append({"atividade": name, "dia": dia, "inicio": ini, "duracao": dur,
                    "turmas": _lst(ev.get("turmas", "")), "professores": _lst(ev.get("professores", ""))})
    return out


class Ctx:
    """Tudo o que as funções de desenho precisam, calculado uma única vez."""

    def __init__(self, result, events, days, n_periods, opts):
        self.opts = dict(opts or {})
        self.days, self.P = list(days), int(n_periods)
        self.color = bool(self.opts.get("colorido", True))
        self.school = str(self.opts.get("escola", "") or "").strip()
        self.subtitle = str(self.opts.get("subtitulo", "") or "").strip()
        self.times = self._times(self.opts.get("horarios"))
        self.stamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        self.reg, self.prev, self.total = {}, {}, None
        res = result.copy() if result is not None else pd.DataFrame()
        ev = _event_rows(events)
        self.classes = sorted({c for v in res.get("turmas", []) for c in _lst(v)} | {c for e in ev for c in e["turmas"]})
        self.teachers = sorted({t for v in res.get("professores", []) for t in _lst(v)} | {t for e in ev for t in e["professores"]})
        subs = sorted({str(d) for d in res.get("disciplina", [])})
        self.cmap = {d: PALETTE[i % len(PALETTE)] for i, d in enumerate(subs)}
        self.entries = {"turma": defaultdict(list), "professor": defaultdict(list)}
        for _, r in res.iterrows():
            ts, ps = _lst(r["turmas"]), _lst(r["professores"])
            base = {"dia": str(r["dia"]), "inicio": int(r["inicio"]), "duracao": int(r["duracao"]),
                    "disciplina": str(r["disciplina"]), "fixed": False}
            for c in ts:
                self.entries["turma"][c].append({**base, "who": ps, "extra": []})
            for p in ps:
                self.entries["professor"][p].append({**base, "who": ts, "extra": [x for x in ps if x != p]})
        for e in ev:
            base = {"dia": e["dia"], "inicio": e["inicio"], "duracao": e["duracao"], "disciplina": e["atividade"], "fixed": True}
            for c in e["turmas"]:
                self.entries["turma"][c].append({**base, "who": e["professores"], "extra": []})
            for p in e["professores"]:
                self.entries["professor"][p].append({**base, "who": e["turmas"], "extra": []})
        # estilos de texto
        ink = colors.HexColor(INK if self.color else "#000000")
        self.st = {
            "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=colors.white if self.color else ink),
            "right": ParagraphStyle("r", fontName="Helvetica", fontSize=9, leading=12, alignment=TA_RIGHT, textColor=colors.white if self.color else ink),
            "hdr": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=9.5, leading=11, alignment=TA_CENTER, textColor=colors.white if self.color else ink),
            "chip": ParagraphStyle("c", fontName="Helvetica", fontSize=8.5, leading=11, textColor=ink),
            "leg": ParagraphStyle("l", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=ink),
            "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=colors.HexColor(NAVY if self.color else "#000000"), spaceBefore=10, spaceAfter=4),
            "body": ParagraphStyle("b", fontName="Helvetica", fontSize=9, leading=12, textColor=ink),
            "idx": ParagraphStyle("i", fontName="Helvetica", fontSize=9, leading=12, textColor=ink),
            "idxr": ParagraphStyle("ir", fontName="Helvetica", fontSize=9, leading=12, alignment=TA_RIGHT, textColor=colors.HexColor(MUTED)),
        }

    @staticmethod
    def _times(cfg):
        """cfg = {'inicio':'07:30','aula':50,'intervalos':[(depois_da_aula, minutos), ...]} -> {periodo: 'HH:MM–HH:MM'}"""
        if not cfg:
            return None
        try:
            h, m = str(cfg.get("inicio", "07:30")).split(":")[:2]
            cur = int(h) * 60 + int(m)
            dur = int(cfg.get("aula", 50))
            brk = {int(a): int(b) for a, b in cfg.get("intervalos", []) if int(b) > 0}
        except Exception:
            return None
        out = {}
        for p in range(1, 41):
            out[p] = f"{cur // 60:02d}:{cur % 60:02d}–{(cur + dur) // 60:02d}:{(cur + dur) % 60:02d}"
            cur += dur + brk.get(p, 0)
        return out

    def fill(self, disc, fixed=False):
        if not self.color:
            return ("#EEEEEE", "#000000") if fixed else ("#FFFFFF", "#000000")
        return FIXED_COLORS if fixed else self.cmap.get(disc, PALETTE[9])


def resource_stats(entries, days, P):
    aulas = sum(e["duracao"] for e in entries if not e["fixed"])
    fixas = sum(e["duracao"] for e in entries if e["fixed"])
    dias, janelas = 0, 0
    for d in days:
        occ = sorted({p for e in entries if e["dia"] == d for p in range(e["inicio"], e["inicio"] + e["duracao"]) if 1 <= p <= P})
        if occ:
            dias += 1
            janelas += (occ[-1] - occ[0] + 1) - len(occ)
    return {"aulas": aulas, "fixas": fixas, "dias": dias, "janelas": janelas}


# ----------------------------------------------------------------------------------
# Peças de desenho
# ----------------------------------------------------------------------------------
class Anchor(Flowable):
    """Marcador invisível: cria o link interno, o marcador (outline) do PDF e registra a página."""

    def __init__(self, ctx, key, title, level=1):
        super().__init__()
        self.ctx, self.key, self.title, self.level = ctx, key, title, level
        self.width = self.height = 0

    def wrap(self, aw, ah):
        return 0, 0

    def draw(self):
        c = self.canv
        c.bookmarkPage(self.key)
        c.addOutlineEntry(self.title, self.key, self.level, closed=(self.level == 0 and False))
        self.ctx.reg[self.key] = c.getPageNumber()


def _p(text, style):
    return Paragraph(text, style)


def _header(ctx, title, right_lines):
    right = "<br/>".join(escape(x) for x in right_lines if x)
    t = Table([[_p(escape(title), ctx.st["title"]), _p(right, ctx.st["right"])]], colWidths=[UW * .62, UW * .38], rowHeights=[46])
    cmds = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14)]
    if ctx.color:
        cmds += [("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY)), ("LINEBELOW", (0, 0), (-1, -1), 3, colors.HexColor(ACCENT))]
    else:
        cmds += [("LINEBELOW", (0, 0), (-1, -1), 1.5, colors.black), ("LEFTPADDING", (0, 0), (0, 0), 0)]
    t.setStyle(TableStyle(cmds))
    return t


def _chips(ctx, texts):
    texts = [t for t in texts if t]
    if not texts:
        return Spacer(1, 1)
    w = min(170, UW / len(texts))
    t = Table([[_p(x, ctx.st["chip"]) for x in texts]], colWidths=[w] * len(texts), rowHeights=[22], hAlign="LEFT")
    cmds = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 8), ("LINEAFTER", (0, 0), (-2, -1), 4, colors.white)]
    cmds.append(("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SOFT if ctx.color else "#F4F4F4")))
    t.setStyle(TableStyle(cmds))
    return t


def _plural(n, s, p):
    return f"{n} {s if n == 1 else p}"


def _cell_text(ctx, es, kind, size=9.5, compact=False):
    parts = []
    for e in es:
        dark = ctx.fill(e["disciplina"], e["fixed"])[1]
        name = escape(e["disciplina"])
        fs = size - (1 if len(e["disciplina"]) > 26 else 0)
        if e["fixed"]:
            sub = "atividade fixa"
        else:
            sub = ", ".join(e["who"])
            if kind == "professor" and e["extra"]:
                sub += " · com " + ", ".join(e["extra"])
        sub = escape(sub)
        sfs = max(6, size - 2.2 - (1 if len(sub) > 34 else 0))
        if compact:
            parts.append(f'<font size="{sfs + .5}" color="{dark}"><b>{name}</b> {sub}</font>')
        else:
            parts.append(f'<font size="{fs}" color="{dark}"><b>{name}</b></font><br/><font size="{sfs}" color="{dark}">{sub}</font>')
    style = ParagraphStyle("cell", fontName="Helvetica", fontSize=size, leading=size + 1.6, alignment=TA_CENTER)
    return Paragraph("<br/>".join(parts), style)


def _time_cell(ctx, p):
    t = ctx.times.get(p) if ctx.times else None
    sub = f'<br/><font size="6.5" color="{MUTED}">{t}</font>' if t else ""
    st = ParagraphStyle("tm", fontName="Helvetica-Bold", fontSize=9.5, leading=11, alignment=TA_CENTER, textColor=colors.HexColor(INK if ctx.color else "#000000"))
    return Paragraph(f"{p}ª{sub}", st)


def _lookup(entries, days, P):
    cells = defaultdict(list)
    for e in entries:
        if e["dia"] not in days:
            continue
        di = days.index(e["dia"])
        for p in range(e["inicio"], e["inicio"] + e["duracao"]):
            if 1 <= p <= P:
                cells[(di, p)].append(e)
    return cells


def _grid(ctx, entries, kind, row_h):
    days, P = ctx.days, ctx.P
    cells = _lookup(entries, days, P)
    tw = 62 if ctx.times else 40
    cw = [tw] + [(UW - tw) / len(days)] * len(days)
    data = [[_p("Aula", ctx.st["hdr"])] + [_p(escape(d), ctx.st["hdr"]) for d in days]]
    cmds = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor(LINE)),
            ("BOX", (0, 0), (-1, -1), .8, colors.HexColor("#B8C2CC")), ("TOPPADDING", (0, 1), (-1, -1), 2), ("BOTTOMPADDING", (0, 1), (-1, -1), 2)]
    cmds.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY if ctx.color else "#DDDDDD")))
    cmds.append(("BACKGROUND", (0, 1), (0, -1), colors.HexColor(SOFT if ctx.color else "#F4F4F4")))
    skip = set()
    spans = {}
    for (di, p), es in list(cells.items()):
        if len(es) == 1 and es[0]["duracao"] > 1 and es[0]["inicio"] == p:
            e = es[0]
            cov = [(di, q) for q in range(p, p + e["duracao"])]
            if all(1 <= q <= P and len(cells.get(k, [])) == 1 and cells[k][0] is e for k, q in zip(cov, range(p, p + e["duracao"]))):
                spans[(di, p)] = e["duracao"]
                skip.update(cov[1:])
    for p in range(1, P + 1):
        row = [_time_cell(ctx, p)]
        for di in range(len(days)):
            es = cells.get((di, p), [])
            if (di, p) in skip or not es:
                row.append("")
            else:
                row.append(_cell_text(ctx, es, kind))
            if es and (di, p) not in skip:
                c, r = di + 1, p
                r2 = r + spans.get((di, p), 1) - 1
                fill, dark = ctx.fill(es[0]["disciplina"], es[0]["fixed"]) if len(es) == 1 else ("#FFE9E9", "#8B2C25")
                cmds.append(("BACKGROUND", (c, r), (c, r2), colors.HexColor(fill)))
                cmds.append(("LINEBEFORE", (c, r), (c, r2), 3 if ctx.color else .8, colors.HexColor(dark if ctx.color else "#000000")))
                if (di, p) in spans:
                    cmds.append(("SPAN", (c, r), (c, r2)))
        data.append(row)
    t = Table(data, colWidths=cw, rowHeights=[26] + [row_h] * P)
    t.setStyle(TableStyle(cmds))
    return t


def _legend(ctx, entries, kind):
    agg = {}
    for e in entries:
        if e["fixed"]:
            continue
        a = agg.setdefault(e["disciplina"], {"who": [], "n": 0})
        a["n"] += e["duracao"]
        for w in e["who"]:
            if w not in a["who"]:
                a["who"].append(w)
    items = sorted(agg.items())
    return items


def _legend_table(ctx, items):
    if not items:
        return Spacer(1, 1), 0
    per = 3
    rows, cw, cmds = [], [], [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]
    for i in range(0, len(items), per):
        chunk = items[i:i + per]
        row = []
        for j in range(per):
            if j < len(chunk):
                d, a = chunk[j]
                txt = f"<b>{escape(d)}</b> · {escape(', '.join(a['who']))} · {_plural(a['n'], 'aula', 'aulas')}"
                row += ["", _p(txt, ctx.st["leg"])]
                fill, dark = ctx.fill(d)
                r = len(rows)
                cmds.append(("BACKGROUND", (j * 2, r), (j * 2, r), colors.HexColor(fill)))
                cmds.append(("BOX", (j * 2, r), (j * 2, r), .8, colors.HexColor(dark if ctx.color else "#000000")))
            else:
                row += ["", ""]
        rows.append(row)
    cw = ([10, UW / per - 10]) * per
    t = Table(rows, colWidths=cw, rowHeights=[15] * len(rows))
    t.setStyle(TableStyle(cmds))
    return t, 15 * len(rows)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def resource_story(ctx, kind, name):
    entries = ctx.entries[kind].get(name, [])
    stt = resource_stats(entries, ctx.days, ctx.P)
    title = f"Turma {name}" if kind == "turma" else f"Prof. {name}"
    right = [ctx.school or "Horário escolar", ctx.subtitle]
    chips = [_plural(stt["aulas"], "aula", "aulas") + "/semana", _plural(stt["dias"], "dia com aula", "dias com aula"),
             _plural(stt["janelas"], "janela", "janelas") + (" (ideal: 0)" if stt["janelas"] else ""),
             _plural(stt["fixas"], "aula fixa", "aulas fixas") if stt["fixas"] else ""]
    leg_items = _legend(ctx, entries, kind)
    leg_tab, leg_h = _legend_table(ctx, leg_items)
    avail = UH - 46 - 8 - 22 - 10 - 26 - 10 - leg_h - 14
    row_h = _clamp(avail / ctx.P, 26, 62)
    story = [_header(ctx, title, right), Spacer(1, 8), _chips(ctx, chips), Spacer(1, 10),
             _grid(ctx, entries, kind, row_h), Spacer(1, 10), leg_tab]
    return story


# ----------------------------------------------------------------------------------
# Quadros gerais
# ----------------------------------------------------------------------------------
def overview_classes_story(ctx, di):
    day, P = ctx.days[di], ctx.P
    cls = ctx.classes
    lk = {}
    for c in cls:
        for e in ctx.entries["turma"].get(c, []):
            if e["dia"] == day:
                for p in range(e["inicio"], e["inicio"] + e["duracao"]):
                    lk.setdefault((c, p), []).append(e)
    n = max(len(cls), 1)
    size = 8.5 if n <= 7 else 7.5 if n <= 10 else 6.5
    tw = 52 if ctx.times else 36
    data = [[_p("Aula", ctx.st["hdr"])] + [_p(escape(c), ctx.st["hdr"]) for c in cls]]
    cmds = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor(LINE)),
            ("BOX", (0, 0), (-1, -1), .8, colors.HexColor("#B8C2CC")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY if ctx.color else "#DDDDDD")),
            ("BACKGROUND", (0, 1), (0, -1), colors.HexColor(SOFT if ctx.color else "#F4F4F4")),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    for p in range(1, P + 1):
        row = [_time_cell(ctx, p)]
        for j, c in enumerate(cls):
            es = lk.get((c, p), [])
            row.append(_cell_text(ctx, es, "turma", size=size) if es else "")
            if es:
                fill, dark = ctx.fill(es[0]["disciplina"], es[0]["fixed"])
                cmds.append(("BACKGROUND", (j + 1, p), (j + 1, p), colors.HexColor(fill)))
                cmds.append(("LINEBEFORE", (j + 1, p), (j + 1, p), 2.5 if ctx.color else .8, colors.HexColor(dark if ctx.color else "#000000")))
        data.append(row)
    rh = _clamp((UH - 46 - 8 - 26 - 16) / P, 28, 66)
    t = Table(data, colWidths=[tw] + [(UW - tw) / n] * len(cls), rowHeights=[26] + [rh] * P)
    t.setStyle(TableStyle(cmds))
    return [_header(ctx, f"Quadro geral · {day}", [ctx.school or "Horário escolar", "Todas as turmas"]), Spacer(1, 8), t]


def overview_teachers_story(ctx, di):
    day, P = ctx.days[di], ctx.P
    tch = ctx.teachers
    lk = {}
    for t_ in tch:
        for e in ctx.entries["professor"].get(t_, []):
            if e["dia"] == day:
                for p in range(e["inicio"], e["inicio"] + e["duracao"]):
                    lk.setdefault((t_, p), []).append(e)
    nw = 104
    data = [[_p("Professor(a)", ctx.st["hdr"])] + [_p(f"{p}ª" + (f'<br/><font size="6.5">{ctx.times[p]}</font>' if ctx.times else ""), ctx.st["hdr"]) for p in range(1, P + 1)]]
    avail = UH - 46 - 8 - 30 - 16
    rh = _clamp(avail / max(len(tch), 1), 20, 40)
    compact = rh < 30
    name_st = ParagraphStyle("nm", fontName="Helvetica-Bold", fontSize=8.5, leading=10, textColor=colors.HexColor(INK if ctx.color else "#000000"))
    cmds = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor(LINE)),
            ("BOX", (0, 0), (-1, -1), .8, colors.HexColor("#B8C2CC")), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY if ctx.color else "#DDDDDD")),
            ("BACKGROUND", (0, 1), (0, -1), colors.HexColor(SOFT if ctx.color else "#F4F4F4")),
            ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]
    for i, t_ in enumerate(tch, start=1):
        row = [_p(escape(t_), name_st)]
        for p in range(1, P + 1):
            es = lk.get((t_, p), [])
            row.append(_cell_text(ctx, es, "professor", size=7.5 if not compact else 7, compact=compact) if es else "")
            if es:
                fill, dark = ctx.fill(es[0]["disciplina"], es[0]["fixed"])
                cmds.append(("BACKGROUND", (p, i), (p, i), colors.HexColor(fill)))
        data.append(row)
    t = Table(data, colWidths=[nw] + [(UW - nw) / P] * P, rowHeights=[30] + [rh] * len(tch), repeatRows=1)
    t.setStyle(TableStyle(cmds))
    return [_header(ctx, f"Professores · {day}", [ctx.school or "Horário escolar", "Onde cada professor está (célula vazia = livre)"]), Spacer(1, 8), t]


# ----------------------------------------------------------------------------------
# Capa e índice
# ----------------------------------------------------------------------------------
def _index_table(ctx, items):
    """items = [(chave, nome)] -> tabela de 3 colunas (nome ........ página) com links."""
    per = 3
    rows = []
    for i in range(0, len(items), per):
        row = []
        for j in range(per):
            if i + j < len(items):
                key, name = items[i + j]
                pg = ctx.prev.get(key, "")
                row += [_p(f'<a href="#{key}" color="{ACCENT if ctx.color else "black"}">{escape(name)}</a>', ctx.st["idx"]), _p(str(pg), ctx.st["idxr"])]
            else:
                row += ["", ""]
        rows.append(row)
    t = Table(rows, colWidths=[(UW / per) - 30, 30] * per)
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), .3, colors.HexColor(LINE)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def cover_story(ctx, turmas, profs, quadros):
    s = [Anchor(ctx, "capa", "Capa e índice", 0)]
    big = ParagraphStyle("big", fontName="Helvetica-Bold", fontSize=30, leading=34, textColor=colors.white if ctx.color else colors.black)
    sm = ParagraphStyle("sm", fontName="Helvetica", fontSize=12, leading=16, textColor=colors.white if ctx.color else colors.black)
    block = [[_p(escape(ctx.school or "Horários escolares"), big)],
             [_p("Horários escolares" + (f" · {escape(ctx.subtitle)}" if ctx.subtitle else "") if ctx.school else escape(ctx.subtitle), sm)],
             [_p(f"{len(turmas)} turmas · {len(profs)} professores · gerado em {ctx.stamp}", sm)]]
    t = Table(block, colWidths=[UW])
    cm = [("LEFTPADDING", (0, 0), (-1, -1), 18), ("TOPPADDING", (0, 0), (0, 0), 16), ("BOTTOMPADDING", (0, -1), (-1, -1), 16)]
    if ctx.color:
        cm += [("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY)), ("LINEBELOW", (0, -1), (-1, -1), 4, colors.HexColor(ACCENT))]
    else:
        cm += [("LINEBELOW", (0, -1), (-1, -1), 2, colors.black), ("LEFTPADDING", (0, 0), (-1, -1), 0)]
    t.setStyle(TableStyle(cm))
    s += [t, Spacer(1, 6), _p("Clique em um nome para ir direto ao horário. O painel de marcadores do leitor de PDF também navega por turma e professor.", ctx.st["body"])]
    if turmas:
        s += [_p("Turmas", ctx.st["h1"]), _index_table(ctx, [(f"turma_{c}", c) for c in turmas])]
    if profs:
        s += [_p("Professores", ctx.st["h1"]), _index_table(ctx, [(f"prof_{p}", p) for p in profs])]
    if quadros:
        items = []
        if "turmas" in quadros:
            items += [(f"qt_{d}", f"Turmas · {d}") for d in ctx.days]
        if "professores" in quadros:
            items += [(f"qp_{d}", f"Professores · {d}") for d in ctx.days]
        s += [_p("Quadros gerais", ctx.st["h1"]), _index_table(ctx, items)]
    return s


# ----------------------------------------------------------------------------------
# Montagem do documento
# ----------------------------------------------------------------------------------
def _footer(ctx, label):
    def draw(canv, doc):
        canv.saveState()
        canv.setStrokeColor(colors.HexColor(LINE)); canv.setLineWidth(.6)
        canv.line(MX, MB - 12, PW - MX, MB - 12)
        canv.setFont("Helvetica", 7.5); canv.setFillColor(colors.HexColor(MUTED))
        left = " · ".join(x for x in [ctx.school, label, f"gerado em {ctx.stamp}"] if x)
        canv.drawString(MX, MB - 24, left)
        pg = f"Página {doc.page}" + (f" de {ctx.total}" if ctx.total else "")
        canv.drawRightString(PW - MX, MB - 24, pg)
        canv.restoreState()
    return draw


def _build(ctx, story, title, footer_label):
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=PAGE, leftMargin=MX, rightMargin=MX, topMargin=MT, bottomMargin=MB,
                          title=title, author=ctx.school or "Gerador de Horários Escolares")
    frame = Frame(MX, MB, UW, UH, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="f")
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=_footer(ctx, footer_label))])
    doc.build(story)
    return buf.getvalue(), doc.page


def make_resource_pdf(result, events, kind, name, days, n_periods, opts=None, ctx=None):
    ctx = ctx or Ctx(result, events, days, n_periods, opts)
    label = f"Turma {name}" if kind == "turma" else f"Prof. {name}"
    data, _ = _build(ctx, resource_story(ctx, kind, name), label, label)
    return data


def make_combined_pdf(result, events, days, n_periods, opts=None, turmas=None, professores=None,
                      capa=True, quadros=("turmas", "professores")):
    ctx = Ctx(result, events, days, n_periods, opts)
    turmas = [c for c in (turmas if turmas is not None else ctx.classes) if c in ctx.classes]
    profs = [p for p in (professores if professores is not None else ctx.teachers) if p in ctx.teachers]
    quadros = tuple(quadros or ())

    def story():
        s = []
        def add(fl):
            if s:
                s.append(PageBreak())
            s.extend(fl)
        if capa:
            add(cover_story(ctx, turmas, profs, quadros))
        for i, c in enumerate(turmas):
            fl = ([Anchor(ctx, "sec_t", "Turmas", 0)] if i == 0 else []) + [Anchor(ctx, f"turma_{c}", f"Turma {c}", 1)]
            add(fl + resource_story(ctx, "turma", c))
        for i, p in enumerate(profs):
            fl = ([Anchor(ctx, "sec_p", "Professores", 0)] if i == 0 else []) + [Anchor(ctx, f"prof_{p}", p, 1)]
            add(fl + resource_story(ctx, "professor", p))
        qs = []
        if "turmas" in quadros:
            qs += [("qt", d, di) for di, d in enumerate(ctx.days)]
        if "professores" in quadros:
            qs += [("qp", d, di) for di, d in enumerate(ctx.days)]
        for i, (k, d, di) in enumerate(qs):
            fl = ([Anchor(ctx, "sec_q", "Quadros gerais", 0)] if i == 0 else []) + [Anchor(ctx, f"{k}_{d}", ("Turmas · " if k == "qt" else "Professores · ") + d, 1)]
            add(fl + (overview_classes_story(ctx, di) if k == "qt" else overview_teachers_story(ctx, di)))
        if not s:
            s.append(_p("Nenhum horário selecionado.", ctx.st["body"]))
        return s

    ctx.reg = {}
    _, total = _build(ctx, story(), "Horários escolares", "Horários escolares")   # 1ª passada: descobre as páginas
    ctx.prev, ctx.total, ctx.reg = dict(ctx.reg), total, {}
    data, _ = _build(ctx, story(), "Horários escolares", "Horários escolares")    # 2ª passada: índice com nº de página
    return data


def safe_name(s):
    return re.sub(r"[^\w\-]+", "_", str(s), flags=re.UNICODE).strip("_") or "sem_nome"


def make_individual_pdfs(result, events, days, n_periods, opts=None, turmas=None, professores=None):
    ctx = Ctx(result, events, days, n_periods, opts)
    out = {}
    for c in (turmas if turmas is not None else ctx.classes):
        out[f"Turma_{safe_name(c)}.pdf"] = make_resource_pdf(result, events, "turma", c, days, n_periods, opts, ctx)
    for p in (professores if professores is not None else ctx.teachers):
        out[f"Professor_{safe_name(p)}.pdf"] = make_resource_pdf(result, events, "professor", p, days, n_periods, opts, ctx)
    return out


# ----------------------------------------------------------------------------------
# Excel com as grades
# ----------------------------------------------------------------------------------
def make_excel_grids(result, events, days, n_periods, opts=None, turmas=None, professores=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    ctx = Ctx(result, events, days, n_periods, opts)
    wb = Workbook(); wb.remove(wb.active)
    thin = Side(style="thin", color="B8C2CC"); border = Border(left=thin, right=thin, top=thin, bottom=thin)
    used = set()

    def sheet_name(prefix, name):
        base = re.sub(r"[\[\]:*?/\\]", "", f"{prefix} {name}")[:31]
        n, i = base, 2
        while n.lower() in used:
            n = f"{base[:28]} {i}"; i += 1
        used.add(n.lower()); return n

    def add(kind, name):
        ws = wb.create_sheet(sheet_name("T" if kind == "turma" else "P", name))
        es = ctx.entries[kind].get(name, [])
        st_ = resource_stats(es, ctx.days, ctx.P)
        ws["A1"] = ("Turma " if kind == "turma" else "Professor(a) ") + name; ws["A1"].font = Font(bold=True, size=14, color=NAVY[1:])
        ws["A2"] = f"{st_['aulas']} aulas/semana · {st_['dias']} dias com aula · {st_['janelas']} janela(s)" + (f" · {ctx.school}" if ctx.school else "")
        ws["A2"].font = Font(size=9, color=MUTED[1:])
        hdr = ["Aula"] + (["Horário"] if ctx.times else []) + list(ctx.days)
        for j, h in enumerate(hdr, 1):
            c = ws.cell(row=4, column=j, value=h); c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor=NAVY[1:])
            c.alignment = Alignment(horizontal="center", vertical="center"); c.border = border
        cells = _lookup(es, ctx.days, ctx.P)
        off = 3 if ctx.times else 2
        for p in range(1, ctx.P + 1):
            r = 4 + p
            ws.cell(row=r, column=1, value=f"{p}ª").alignment = Alignment(horizontal="center", vertical="center")
            ws.cell(row=r, column=1).font = Font(bold=True)
            if ctx.times:
                ws.cell(row=r, column=2, value=ctx.times.get(p)).alignment = Alignment(horizontal="center", vertical="center")
            for di in range(len(ctx.days)):
                e_ = cells.get((di, p), [])
                txt = "\n".join((x["disciplina"] + "\n" + (", ".join(x["who"]) if not x["fixed"] else "atividade fixa")) for x in e_)
                c = ws.cell(row=r, column=off + di, value=txt)
                c.alignment = Alignment(wrap_text=True, horizontal="center", vertical="center"); c.border = border
                if e_:
                    c.fill = PatternFill("solid", fgColor=ctx.fill(e_[0]["disciplina"], e_[0]["fixed"])[0][1:])
                    c.font = Font(color=ctx.fill(e_[0]["disciplina"], e_[0]["fixed"])[1][1:], size=10)
            ws.row_dimensions[r].height = 44
        ws.column_dimensions["A"].width = 7
        if ctx.times: ws.column_dimensions["B"].width = 13
        for di in range(len(ctx.days)):
            ws.column_dimensions[get_column_letter(off + di)].width = 24
        ws.sheet_view.showGridLines = False
        ws.page_setup.orientation = "landscape"; ws.page_setup.fitToWidth = 1; ws.sheet_properties.pageSetUpPr.fitToPage = True

    for c in (turmas if turmas is not None else ctx.classes): add("turma", c)
    for p in (professores if professores is not None else ctx.teachers): add("professor", p)
    b = io.BytesIO(); wb.save(b); return b.getvalue()


# ----------------------------------------------------------------------------------
# Tabelas auxiliares (cargas, quem está livre)
# ----------------------------------------------------------------------------------
def load_report(result, events, days, n_periods):
    ctx = Ctx(result, events, days, n_periods, {})
    rows = []
    for typ, kind, names in [("Turma", "turma", ctx.classes), ("Professor", "professor", ctx.teachers)]:
        for name in names:
            es = ctx.entries[kind].get(name, [])
            lessons = [e for e in es if not e["fixed"]]
            stt = resource_stats(es, ctx.days, ctx.P)
            byday = {d: sum(e["duracao"] for e in lessons if e["dia"] == d) for d in ctx.days}
            rows.append({"Tipo": typ, "Nome": name, "Aulas/semana": stt["aulas"], **byday,
                         "Dias com aula": stt["dias"], "Janelas": stt["janelas"]})
    return pd.DataFrame(rows)


def _truthy(v):
    return v is True or str(v).strip().lower() in ("true", "1", "sim", "yes", "verdadeiro", "x")


def free_teachers(result, events, day, period, all_teachers, blocks=None, n_periods=None):
    """Situação de cada professor numa célula (dia, aula) – útil para escolher substitutos."""
    ctx = Ctx(result, events, [day], period if n_periods is None else n_periods, {})
    rows = []
    for t in sorted(set(all_teachers) | set(ctx.teachers)):
        status, where = "Livre", ""
        for e in ctx.entries["professor"].get(t, []):
            if e["dia"] == day and e["inicio"] <= period < e["inicio"] + e["duracao"]:
                status = "Atividade fixa" if e["fixed"] else "Em aula"
                where = f"{e['disciplina']} · {', '.join(e['who'])}" if not e["fixed"] else e["disciplina"]
                break
        if status == "Livre" and blocks is not None and len(blocks):
            for _, b in blocks.fillna("").iterrows():
                if str(b.get("professor", "")).strip() == t and str(b.get("dia", "")).strip() == day:
                    if _truthy(b.get("dia_inteiro")) or _num(b.get("aula")) == period:
                        status, where = "Bloqueado", "restrição cadastrada"
                        break
        rows.append({"Professor": t, "Situação": status, "Onde": where})
    df = pd.DataFrame(rows)
    order = {"Livre": 0, "Bloqueado": 1, "Atividade fixa": 2, "Em aula": 3}
    return df.sort_values(["Situação", "Professor"], key=lambda s: s.map(order) if s.name == "Situação" else s).reset_index(drop=True)
