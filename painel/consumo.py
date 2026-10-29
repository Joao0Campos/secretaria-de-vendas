"""Consumo real da IA (Gemini), medido chamada a chamada (21/10/2026).

O saldo pré-pago do Gemini acabou; para saber quanto a ferramenta gasta de
verdade, cada leitura de documento grava UMA linha em
painel_dados/consumo_ia-AAAA-MM.jsonl (um arquivo por mês, 13 meses guardados):

    {"em", "origem" (varredura|reanalise|releitura|outro), "idreserva", "tipo_cv",
     "paginas", "modelo", "do_cache", "erro", "entrada", "saida", "raciocinio", "total"}

- entrada/saida/raciocinio/total = usage_metadata da resposta (prompt_token_count,
  candidates_token_count, thoughts_token_count, total_token_count). Acerto do
  cache = do_cache=true e tokens 0: é a economia.
- SEM dado pessoal: nada de nome, CPF, texto lido ou nome de arquivo (o nome do
  arquivo e da pasta trazem o cliente). tipo_cv só de uma lista FECHADA
  (TIPOS_CV; o resto vira "outro"). idreserva é o número interno da reserva, o
  mesmo da planilha e do painel (decisão do gerente, revisão da Turing): não
  identifica ninguém fora do sistema, que já o mostra em toda tela.
- Escrita: O_APPEND, laço até a linha inteira ir para o disco, trava entre as
  threads do processo e entre processos (um .lock com O_EXCL, espera curta).
  Falha no registro NUNCA quebra a análise: a linha se perde, a leitura não.

Custo: preço POR MODELO (PRECOS_POR_MODELO, com a data do preço), com override
por variável de ambiente. Modelo sem preço conhecido = custo "desconhecido",
nunca um preço errado. O raciocínio é cobrado como saída.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

ORIGENS = ("varredura", "reanalise", "releitura", "outro")
CAMPOS = ("em", "origem", "idreserva", "tipo_cv", "paginas", "modelo", "do_cache", "erro",
          "entrada", "saida", "raciocinio", "total")
# Os tipos de documento do CV (os que existiam nas leituras em 21/10/2026, mais o CTPS).
TIPOS_CV = ("RG, CPF, CNH ou outros", "Comprovante de Renda", "Certidão de Estado Civil",
            "Comprovante de Endereço", "Extrato de FGTS", "Simulação Inicial (Comercial VIC)",
            "Tela de Aprovação", "MO Pesquisa Cadastral", "Documentos Diversos", "Imposto de Renda + Recibo",
            "Simulação CCA", "Formulários CEF Emitido", "Comprovante de pagto ATO", "Unidade Adaptada", "CTPS")
MESES_GUARDADOS = 13
RE_ARQUIVO = re.compile(r"consumo_ia-(\d{4})-(\d{2})\.jsonl")

# US$ por milhão de tokens (tabela pública do Gemini; o raciocínio é cobrado como saída).
# Override por modelo: SECRETARIA_PRECO_<MODELO> = "entrada/saida[/data]", com o nome
# em maiúsculas e '-'/'.' trocados por '_' (ex.: SECRETARIA_PRECO_GEMINI_2_5_FLASH=0.30/2.50/01/11/2026).
PRECOS_POR_MODELO = {
    "gemini-2.5-flash": {"entrada": 0.30, "saida": 2.50, "data": "21/10/2026"},
    "gemini-2.5-flash-lite": {"entrada": 0.10, "saida": 0.40, "data": "21/10/2026"},
    "gemini-2.5-pro": {"entrada": 1.25, "saida": 10.00, "data": "21/10/2026 (prompt até 200 mil tokens)"},
}
COTACAO_PADRAO = 5.50  # R$ por US$ (SECRETARIA_COTACAO_USD_BRL)
ESPERA_DA_TRAVA_S = 2.0
TRAVA_VELHA_APOS_S = 10.0
_trava = threading.Lock()


def _inteiro(valor) -> int:
    try:
        return max(0, int(valor or 0))
    except (TypeError, ValueError):
        return 0


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)).casefold()


_TIPOS_NORMALIZADOS = {_sem_acento(t).strip(): t for t in TIPOS_CV}


def tipo_conhecido(tipo_cv) -> str:
    """O tipo canônico da lista fechada, ou "outro" (texto livre nunca vai para o arquivo)."""
    return _TIPOS_NORMALIZADOS.get(_sem_acento(str(tipo_cv or "")).strip(), "outro")


def arquivo_do_mes(pasta: Path, quando: datetime) -> Path:
    return Path(pasta) / f"consumo_ia-{quando:%Y-%m}.jsonl"


def _meses_para_tras(quando: datetime, n: int) -> tuple[int, int]:
    total = quando.year * 12 + (quando.month - 1) - n
    return total // 12, total % 12 + 1


def _apagar_meses_velhos(pasta: Path, agora: datetime) -> None:
    ano, mes = _meses_para_tras(agora, MESES_GUARDADOS - 1)  # o mais velho que fica
    for p in Path(pasta).glob("consumo_ia-*.jsonl"):
        m = RE_ARQUIVO.fullmatch(p.name)
        if m and (int(m.group(1)), int(m.group(2))) < (ano, mes):
            p.unlink(missing_ok=True)


def _pegar_trava(trava: Path) -> bool:
    limite = time.monotonic() + ESPERA_DA_TRAVA_S
    while True:
        try:
            os.close(os.open(trava, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - trava.stat().st_mtime > TRAVA_VELHA_APOS_S:  # dono morreu no meio
                    trava.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
        if time.monotonic() >= limite:
            return False
        time.sleep(0.02)


def _gravar_linha(destino: Path, dados: bytes) -> None:
    fd = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0))
    try:
        enviado = 0
        while enviado < len(dados):  # escrita parcial: continua de onde parou
            n = os.write(fd, dados[enviado:])
            if n <= 0:
                raise OSError("escrita sem progresso")
            enviado += n
    finally:
        os.close(fd)


def registrar(pasta: Path, *, origem: str, idreserva: int | None, tipo_cv: str, paginas: int = 0,
              modelo: str = "", do_cache: bool = False, erro: bool = False, uso=None,
              agora: datetime | None = None) -> bool:
    """Grava uma linha no arquivo do mês, em 'pasta' (painel_dados). 'uso' =
    resp.usage_metadata (ou None). Nunca levanta exceção; False = não gravou."""
    try:
        agora = agora or datetime.now()
        linha = {
            "em": agora.strftime("%Y-%m-%d %H:%M:%S"),
            "origem": origem if origem in ORIGENS else "outro",
            "idreserva": int(idreserva) if isinstance(idreserva, int) or str(idreserva or "").isdigit() else None,
            "tipo_cv": tipo_conhecido(tipo_cv),
            "paginas": _inteiro(paginas),
            "modelo": str(modelo or "")[:60],
            "do_cache": bool(do_cache),
            "erro": bool(erro),
            "entrada": _inteiro(getattr(uso, "prompt_token_count", 0)),
            "saida": _inteiro(getattr(uso, "candidates_token_count", 0)),
            "raciocinio": _inteiro(getattr(uso, "thoughts_token_count", 0)),
            "total": _inteiro(getattr(uso, "total_token_count", 0)),
        }
        dados = (json.dumps({c: linha[c] for c in CAMPOS}, ensure_ascii=False) + "\n").encode("utf-8")
        pasta = Path(pasta)
        destino = arquivo_do_mes(pasta, agora)
        with _trava:
            pasta.mkdir(parents=True, exist_ok=True)
            trava = pasta / "consumo_ia.lock"
            if not _pegar_trava(trava):
                return False
            try:
                mes_novo = not destino.exists()
                _gravar_linha(destino, dados)
                if mes_novo:
                    _apagar_meses_velhos(pasta, agora)
            finally:
                trava.unlink(missing_ok=True)
        return True
    except Exception:  # medir o consumo nunca pode derrubar a leitura do documento
        return False


def ler(pasta: Path, meses: list[tuple[int, int]]) -> list[dict]:
    """As linhas válidas dos meses pedidos ([(ano, mês)]); linha quebrada é ignorada."""
    linhas = []
    for ano, mes in meses:
        try:
            texto = (Path(pasta) / f"consumo_ia-{ano:04d}-{mes:02d}.jsonl").read_text(encoding="utf-8")
        except OSError:
            continue
        for bruto in texto.splitlines():
            try:
                item = json.loads(bruto)
                item["_em"] = datetime.strptime(item["em"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError, TypeError):
                continue
            linhas.append(item)
    return linhas


def meses_da_tela(agora: datetime | None = None) -> list[tuple[int, int]]:
    """A tela mostra o mês atual e o anterior (os 7 dias cabem nos dois)."""
    agora = agora or datetime.now()
    return [(agora.year, agora.month), _meses_para_tras(agora, 1)]


# ------------------------------------------------------------------ preços e custo
def _variavel_do_modelo(modelo: str) -> str:
    return "SECRETARIA_PRECO_" + re.sub(r"[^A-Z0-9]", "_", modelo.upper())


def precos() -> dict:
    """{'modelos': {modelo: {entrada, saida, data, origem}}, 'cotacao_usd_brl'}."""
    modelos = {m: dict(p, origem="tabela") for m, p in PRECOS_POR_MODELO.items()}
    for nome, valor in os.environ.items():
        if not nome.startswith("SECRETARIA_PRECO_") or nome == "SECRETARIA_PRECO_DATA":
            continue
        partes = (valor or "").strip().split("/", 2)
        try:
            entrada, saida = (float(x.replace(",", ".")) for x in partes[:2])
        except ValueError:
            continue
        if entrada < 0 or saida < 0:
            continue
        modelo = next((m for m in modelos if _variavel_do_modelo(m) == nome), None)
        modelo = modelo or nome[len("SECRETARIA_PRECO_"):].lower().replace("_", "-")
        modelos[modelo] = {"entrada": entrada, "saida": saida, "origem": f"variável {nome}",
                           "data": partes[2].strip() if len(partes) > 2 and partes[2].strip() else "(sem data)"}
    try:
        cotacao = float((os.environ.get("SECRETARIA_COTACAO_USD_BRL") or "").replace(",", "."))
        cotacao = cotacao if cotacao > 0 else COTACAO_PADRAO
    except ValueError:
        cotacao = COTACAO_PADRAO
    return {"modelos": modelos, "cotacao_usd_brl": cotacao}


def custo_usd(entrada: int, saida: int, raciocinio: int, preco: dict | None) -> float | None:
    """None = modelo sem preço conhecido."""
    if not preco:
        return None
    return (entrada * preco["entrada"] + (saida + raciocinio) * preco["saida"]) / 1_000_000


def somar(linhas: list[dict], p: dict | None = None) -> dict:
    p = p or precos()
    ia = [l for l in linhas if not l.get("do_cache")]
    entrada = sum(_inteiro(l.get("entrada")) for l in ia)
    saida = sum(_inteiro(l.get("saida")) for l in ia)
    raciocinio = sum(_inteiro(l.get("raciocinio")) for l in ia)
    usd, sem_preco = 0.0, set()
    for l in ia:
        c = custo_usd(_inteiro(l.get("entrada")), _inteiro(l.get("saida")), _inteiro(l.get("raciocinio")),
                      p["modelos"].get(str(l.get("modelo") or "")))
        if c is None:
            sem_preco.add(str(l.get("modelo") or "(sem modelo)"))
        else:
            usd += c
    conhecido = not sem_preco
    reservas = {l.get("idreserva") for l in linhas if l.get("idreserva") is not None}
    documentos = len(ia)
    brl = usd * p["cotacao_usd_brl"]

    def por(valor, n, casas):
        return round(valor / n, casas) if conhecido and n else None
    return {
        "chamadas": documentos, "do_cache": len(linhas) - documentos, "erros": sum(1 for l in ia if l.get("erro")),
        "entrada": entrada, "saida": saida, "raciocinio": raciocinio,
        "usd": round(usd, 4) if conhecido else None, "brl": round(brl, 2) if conhecido else None,
        "modelos_sem_preco": sorted(sem_preco), "reservas": len(reservas),
        "usd_por_documento": por(usd, documentos, 5), "usd_por_reserva": por(usd, len(reservas), 4),
        "brl_por_documento": por(brl, documentos, 4), "brl_por_reserva": por(brl, len(reservas), 3),
    }


def agregar(linhas: list[dict], agora: datetime | None = None, p: dict | None = None) -> dict:
    """Hoje, últimos 7 dias, mês atual e mês anterior, e a projeção do mês: o
    gasto do mês até agora + a média diária dos últimos 7 dias x os dias que faltam."""
    agora = agora or datetime.now()
    p = p or precos()
    hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_mes = hoje.replace(day=1)
    inicio_mes_anterior = (inicio_mes - timedelta(days=1)).replace(day=1)
    sete = hoje - timedelta(days=6)
    periodos = {
        "hoje": [l for l in linhas if l["_em"] >= hoje],
        "sete_dias": [l for l in linhas if l["_em"] >= sete],
        "mes_atual": [l for l in linhas if l["_em"] >= inicio_mes],
        "mes_anterior": [l for l in linhas if inicio_mes_anterior <= l["_em"] < inicio_mes],
    }
    resumo = {nome: somar(itens, p) for nome, itens in periodos.items()}
    dias_no_mes = calendar.monthrange(agora.year, agora.month)[1]
    restantes = dias_no_mes - agora.day
    if resumo["sete_dias"]["usd"] is None or resumo["mes_atual"]["usd"] is None:
        resumo["projecao_mes"] = {"usd": None, "brl": None, "media_diaria_usd": None, "dias_restantes": restantes}
    else:
        media_dia = resumo["sete_dias"]["usd"] / 7
        projecao = resumo["mes_atual"]["usd"] + media_dia * restantes
        resumo["projecao_mes"] = {"usd": round(projecao, 2), "brl": round(projecao * p["cotacao_usd_brl"], 2),
                                  "media_diaria_usd": round(media_dia, 4), "dias_restantes": restantes}
    resumo["precos"] = p
    return resumo


def resumo_desde(pasta: Path, desde: datetime, origem: str | None = None) -> dict:
    """Soma das linhas a partir de 'desde' (ex.: a linha final da varredura)."""
    agora = datetime.now()
    meses, (ano, mes) = [], (desde.year, desde.month)
    while (ano, mes) <= (agora.year, agora.month):
        meses.append((ano, mes))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    linhas = [l for l in ler(pasta, meses) if l["_em"] >= desde.replace(microsecond=0)
              and (origem is None or l.get("origem") == origem)]
    return somar(linhas)
