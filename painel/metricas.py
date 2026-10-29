"""Métricas de impacto da ferramenta, a partir das planilhas e das tratativas.

Definições (escritas aqui porque é o que vai para a diretoria):
- "novos no período" = apontamentos cujo primeiro_visto (data da planilha onde
  apareceram pela 1ª vez) cai no período. Contagens, status e categorias são
  sobre eles.
- "resolvido" = status atual corrigido ou resolvido_na_reanalise. Ignorado NÃO
  é resolvido: é a pessoa dizendo que o apontamento não era problema.
- taxa_resolucao = resolvidos / (novos - ignorados), em %: de tudo que era
  problema de verdade, quanto já saiu da fila.
- horas_ate_resolver e as contagens por evento (resolvidos por semana,
  ignorados por motivo, tratativas por usuário) usam a DATA DO EVENTO no período.
- pct_falso_positivo = ignorados com motivo "Falso positivo da IA" / total da
  categoria, em %.
"""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timedelta

from .banco import (
    MOTIVO_FALSO_POSITIVO,
    MOTIVOS_IGNORAR,
    STATUS_ABERTO,
    STATUS_CORRIGIDO,
    STATUS_IGNORADO,
    STATUS_RESOLVIDO_NA_REANALISE,
    STATUS_VALIDOS,
    USUARIO_SISTEMA,
    agora as agora_padrao,
    de_texto,
)
from .dados import NIVEL_AVISO, NIVEL_DIVERGENCIA, STATUS_DIVERGENCIA, Reserva

PERIODOS = ("7", "30", "90", "tudo")
PERIODO_PADRAO = "30"
STATUS_RESOLVIDOS = (STATUS_CORRIGIDO, STATUS_RESOLVIDO_NA_REANALISE)
MAXIMO_SEMANAS = 12


def periodo_de(valor) -> str:
    valor = str(valor or "").strip()
    return valor if valor in PERIODOS else PERIODO_PADRAO


def inicio_do_periodo(periodo: str, agora: datetime) -> datetime | None:
    return None if periodo == "tudo" else agora - timedelta(days=int(periodo))


def _pct(parte: int, todo: int) -> float | None:
    return round(100 * parte / todo, 1) if todo else None


def _segunda_feira(momento: datetime) -> datetime:
    dia = momento.replace(hour=0, minute=0, second=0, microsecond=0)
    return dia - timedelta(days=dia.weekday())


def _grupo_do_motivo(motivo: str | None) -> str:
    if motivo in MOTIVOS_IGNORAR:
        return motivo
    return "Outro" if motivo else "(sem motivo)"


def calcular(
    reservas: list[Reserva], apontamentos: list[dict], eventos: list[dict],
    periodo: str = PERIODO_PADRAO, minutos_manuais: int | None = None, agora: datetime | None = None,
) -> dict:
    agora = agora or agora_padrao()
    periodo = periodo_de(periodo)
    inicio = inicio_do_periodo(periodo, agora)

    def no_periodo(momento: datetime | None) -> bool:
        return momento is not None and (inicio is None or momento >= inicio)

    # ---- volume
    analisadas = [r for r in reservas if no_periodo(r.analisado_em)]
    pastas = [p for r in analisadas for p in r.pastas if p.analisada and no_periodo(p.analisado_em)]
    com_divergencia = sum(1 for r in analisadas if r.status == STATUS_DIVERGENCIA)

    # ---- status atual de cada apontamento (último evento)
    ultimo: dict[str, dict] = {}
    for ev in eventos:  # vêm em ordem de id
        ultimo[ev["chave"]] = ev

    def status_de(a: dict) -> str:
        return (ultimo.get(a["chave"]) or {}).get("status", STATUS_ABERTO)

    novos = [a for a in apontamentos if no_periodo(de_texto(a["primeiro_visto"]))]
    por_status = Counter(status_de(a) for a in novos)
    ignorados = por_status[STATUS_IGNORADO]
    resolvidos = sum(por_status[s] for s in STATUS_RESOLVIDOS)

    # ---- tempo até resolver (pela data da resolução)
    horas = []
    for a in apontamentos:
        ev = ultimo.get(a["chave"])
        if not ev or ev["status"] not in STATUS_RESOLVIDOS:
            continue
        em = de_texto(ev["em"])
        if not no_periodo(em):
            continue
        horas.append(max(0.0, (em - de_texto(a["primeiro_visto"])).total_seconds() / 3600))
    horas_ate_resolver = (
        {"media": round(statistics.fmean(horas), 1), "mediana": round(statistics.median(horas), 1)}
        if horas else None)

    # ---- por categoria
    categorias: dict[str, dict] = {}
    for a in novos:
        c = categorias.setdefault(a["categoria"], {
            "categoria": a["categoria"], "total": 0, "divergencias": 0, "avisos": 0,
            "corrigidos": 0, "ignorados": 0, "resolvidos_na_reanalise": 0, "abertos": 0,
            "_falso_positivo": 0})
        c["total"] += 1
        c["divergencias"] += a["nivel"] == NIVEL_DIVERGENCIA
        c["avisos"] += a["nivel"] == NIVEL_AVISO
        s = status_de(a)
        c["corrigidos"] += s == STATUS_CORRIGIDO
        c["ignorados"] += s == STATUS_IGNORADO
        c["resolvidos_na_reanalise"] += s == STATUS_RESOLVIDO_NA_REANALISE
        c["abertos"] += s == STATUS_ABERTO
        c["_falso_positivo"] += (s == STATUS_IGNORADO
                                 and (ultimo.get(a["chave"]) or {}).get("motivo") == MOTIVO_FALSO_POSITIVO)
    por_categoria = []
    for c in sorted(categorias.values(), key=lambda c: (-c["total"], c["categoria"])):
        c["pct_falso_positivo"] = _pct(c.pop("_falso_positivo"), c["total"])
        por_categoria.append(c)

    # ---- por semana (até 12, terminando na semana de hoje)
    fim = _segunda_feira(agora)
    datas = [de_texto(a["primeiro_visto"]) for a in apontamentos]
    if inicio is not None:
        comeco = _segunda_feira(inicio)
    else:
        comeco = _segunda_feira(min(datas)) if datas else fim
    comeco = max(comeco, fim - timedelta(weeks=MAXIMO_SEMANAS - 1))
    semanas = []
    semana = comeco
    while semana <= fim:
        proxima = semana + timedelta(weeks=1)
        n_novos = sum(1 for d in datas if semana <= d < proxima and no_periodo(d))
        n_resolvidos = 0
        for ev in ultimo.values():
            if ev["status"] in STATUS_RESOLVIDOS:
                em = de_texto(ev["em"])
                n_resolvidos += semana <= em < proxima and no_periodo(em)
        semanas.append({"semana": semana.strftime("%d/%m"), "novos": n_novos, "resolvidos": n_resolvidos})
        semana = proxima

    # ---- ignorados por motivo e tratativas por usuário (pela data do evento)
    motivos = Counter(
        _grupo_do_motivo(ev.get("motivo")) for ev in ultimo.values()
        if ev["status"] == STATUS_IGNORADO and no_periodo(de_texto(ev["em"])))
    usuarios = Counter(
        ev["usuario"] for ev in eventos
        if ev["usuario"] != USUARIO_SISTEMA and no_periodo(de_texto(ev["em"])))

    return {
        "reservas_analisadas": len(analisadas),
        "pessoas": len(pastas),
        "documentos_lidos": sum(p.documentos_lidos for p in pastas),
        "apontamentos": len(novos),
        "divergencias": sum(1 for a in novos if a["nivel"] == NIVEL_DIVERGENCIA),
        "avisos": sum(1 for a in novos if a["nivel"] == NIVEL_AVISO),
        "pct_reservas_com_divergencia": _pct(com_divergencia, len(analisadas)),
        "por_status": {s: por_status[s] for s in STATUS_VALIDOS},
        "taxa_resolucao": _pct(resolvidos, len(novos) - ignorados),
        "horas_ate_resolver": horas_ate_resolver,
        "por_categoria": por_categoria,
        "por_semana": semanas,
        "ignorados_por_motivo": [{"motivo": m, "n": n} for m, n in motivos.most_common()],
        "por_usuario": [{"usuario": u, "tratativas": n} for u, n in usuarios.most_common()],
        "tempo_economizado_horas": (round(minutos_manuais * len(analisadas) / 60, 1)
                                    if minutos_manuais else None),
    }
