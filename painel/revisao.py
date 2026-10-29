"""Revisão de regras: os apontamentos que alguém ignorou, por categoria.

É a fila de correção de regra. Daqui nada muda sozinho - nem regra nem
status: quem ignora pode ter interesse em destravar a venda, então o ignorado é
uma pista para alguém conferir, não um voto que ajusta a auditoria.

Definições:
- entra só apontamento cujo status ATUAL é ignorado (reaberto sai da fila);
- o período vale pela data do evento de ignorar (o último evento dele);
- pct_falso_positivo é o mesmo número da página de impacto, para o mesmo
  período (ver metricas.calcular).
Sem nome de cliente nem nome de arquivo/pasta: só o papel na operação e links
opacos.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from analisar_documentos import ROTULOS_DE_SENTINELA

from .banco import (
    MOTIVO_FALSO_POSITIVO,
    MOTIVO_REGRA_NAO_SE_APLICA,
    STATUS_IGNORADO,
    agora as agora_padrao,
    de_texto,
)
from .dados import (
    Pasta,
    Reserva,
    formatar_momento,
    mascarar_no_texto,
    mascarar_texto_livre,
    url_cv,
    url_do_arquivo,
)
from .metricas import PERIODOS, _grupo_do_motivo, calcular, inicio_do_periodo

# filtro -> motivos aceitos (None = qualquer um)
FILTROS_MOTIVO = {
    "fp_regra": (MOTIVO_FALSO_POSITIVO, MOTIVO_REGRA_NAO_SE_APLICA),
    "fp": (MOTIVO_FALSO_POSITIVO,),
    "regra": (MOTIVO_REGRA_NAO_SE_APLICA,),
    "todos": None,
}
MOTIVO_PADRAO = "fp_regra"
PERIODO_PADRAO = "tudo"


def filtro_de(params) -> dict:
    motivo = str(params.get("motivo") or "").strip()
    periodo = str(params.get("periodo") or "").strip()
    return {"motivo": motivo if motivo in FILTROS_MOTIVO else MOTIVO_PADRAO,
            "periodo": periodo if periodo in PERIODOS else PERIODO_PADRAO}


def _arquivo_original(pasta: Pasta, a: dict) -> str | None:
    """Nome do arquivo como está na pasta. No banco ele fica mascarado; o
    apontamento ainda presente diz o nome pela chave, e o que sumiu da planilha
    é achado comparando as máscaras dos arquivos que a pasta conhece."""
    for b in pasta.apontamentos:
        if b["chave"] == a["chave"]:
            return b["arquivo"]
    if a["arquivo"] in ROTULOS_DE_SENTINELA:
        return a["arquivo"]
    conhecidos = set(pasta.resultados)
    conhecidos |= {str(d["arquivo_local"]) for d in pasta.documentos if d.get("arquivo_local")}
    for nome in sorted(conhecidos):
        if mascarar_no_texto(nome) == a["arquivo"]:
            return nome
    return None


def _tipos_do_arquivo(pasta: Pasta, arquivo: str | None) -> tuple[str | None, str | None]:
    """(tipo no CV, tipo que a IA leu) - como na página da reserva."""
    if not arquivo or arquivo in ROTULOS_DE_SENTINELA:
        return None, None
    r = pasta.resultados.get(arquivo) or {}
    tipo_cv = next((d.get("tipo") for d in pasta.documentos if d.get("arquivo_local") == arquivo), None)
    tipo_ia = str((r.get("doc_ia") or {}).get("tipo_identificado") or "")
    return (tipo_cv or r.get("tipo_cv") or None), (tipo_ia or None)


def _caso(a: dict, ev: dict, em: datetime, pasta: Pasta | None, modelos_cv: dict) -> dict:
    arquivo = _arquivo_original(pasta, a) if pasta else None
    tipo_cv, tipo_ia = _tipos_do_arquivo(pasta, arquivo) if pasta else (None, None)
    return {
        "chave": a["chave"], "tipo": a["tipo"], "id": a["id_reserva"],
        "papel": pasta.papel if pasta else None,
        "tipo_cv": tipo_cv, "tipo_ia": tipo_ia,
        "nivel": a["nivel"], "texto": a["texto"],
        "motivo": ev.get("motivo"), "detalhe": mascarar_texto_livre(ev.get("detalhe")),
        "responsavel": ev.get("usuario"), "atualizado_em": formatar_momento(em),
        "url_reserva": f"/reserva/{a['tipo']}/{a['id_reserva']}",
        "url_cv": url_cv(modelos_cv.get(a["tipo"]), a["id_reserva"]),
        "url_arquivo": url_do_arquivo(pasta, arquivo) if pasta else None,
        "presente": bool(a["presente"]),
    }


def montar(reservas: list[Reserva], apontamentos: list[dict], eventos: list[dict],
           filtro: dict, agora: datetime | None = None, modelos_cv: dict | None = None) -> dict:
    """'apontamentos' e 'eventos': linhas do banco (eventos em ordem de id).
    'modelos_cv': {tipo: modelo do link "Abrir no CV"}."""
    agora = agora or agora_padrao()
    inicio = inicio_do_periodo(filtro["periodo"], agora)
    aceitos = FILTROS_MOTIVO[filtro["motivo"]]

    ultimo: dict[str, dict] = {}
    for ev in eventos:
        ultimo[ev["chave"]] = ev
    pastas = {(p.tipo, p.nome_pasta): p for r in reservas for p in r.pastas}

    por_categoria: dict[str, list[tuple[datetime, str, dict]]] = {}
    for a in apontamentos:
        ev = ultimo.get(a["chave"])
        if not ev or ev["status"] != STATUS_IGNORADO:
            continue
        if aceitos is not None and ev.get("motivo") not in aceitos:
            continue
        em = de_texto(ev["em"])
        if inicio is not None and em < inicio:
            continue
        por_categoria.setdefault(a["categoria"], []).append(
            (em, a["chave"], _caso(a, ev, em, pastas.get((a["tipo"], a["pasta"])), modelos_cv or {})))

    pct = {c["categoria"]: c["pct_falso_positivo"]
           for c in calcular(reservas, apontamentos, eventos, filtro["periodo"], None, agora)["por_categoria"]}

    grupos = []
    for categoria, itens in por_categoria.items():
        itens.sort(key=lambda t: (t[0], t[1]), reverse=True)
        casos = [caso for _, _, caso in itens]
        motivos = Counter(_grupo_do_motivo(c["motivo"]) for c in casos)
        grupos.append({
            "categoria": categoria, "total": len(casos),
            "por_motivo": [{"motivo": m, "n": n}
                           for m, n in sorted(motivos.items(), key=lambda mn: (-mn[1], mn[0]))],
            "pct_falso_positivo": pct.get(categoria),
            "casos": casos,
        })
    grupos.sort(key=lambda g: (-g["total"], g["categoria"]))

    casos = [c for g in grupos for c in g["casos"]]
    return {
        "filtro": filtro,
        "totais": {"casos": len(casos), "categorias": len(grupos),
                   "sem_detalhe": sum(1 for c in casos if not c["detalhe"])},
        "grupos": grupos,
    }
