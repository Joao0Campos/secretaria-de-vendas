"""Falhas da IA (Gemini) e o disjuntor (desenho da Turing, 21/10/2026).

Uma falha de SISTEMA (sem saldo, limite, rede, configuração, resposta inválida)
não é achado sobre o documento: nunca vira divergência, apontamento ou categoria
do painel. O documento fica "aguardando leitura" (aba "Falhas da IA" da
planilha), não entra no cache e é lido de novo na rodada seguinte.

Classes (FalhaIA.classe):
- limite_ou_saldo: 429 por spending cap, conta de faturamento, crédito/saldo
  pré-pago ou cota diária/mensal. ARMA o disjuntor.
- rate_limit: 429 com o marcador explícito de "por minuto". Backoff curto e
  limitado; NÃO arma o disjuntor.
- indeterminado_429: 429 sem marcador reconhecido. Sem nova tentativa; 3
  seguidos na mesma rodada armam o disjuntor como "indeterminado" (revisão da
  Turing: pode ser limite ou saldo, confira no AI Studio).
- rede_timeout_5xx: rede, timeout, 5xx.
- configuracao: chave inválida, sem permissão, modelo inexistente (401/403/400/404).
- resposta_invalida: a IA respondeu, mas sem um objeto JSON utilizável
  (cortada pelo limite de tokens, JSON inválido, lista...).
- bloqueada: a chamada nem foi feita, porque o disjuntor está armado (ou o
  estado está ilegível). Não arma de novo.
O detalhe técnico, sanitizado (sem chave), vai só para o log.

Disjuntor: painel_dados/ia_estado.json no PC, gravado de forma atômica:
{"estado": "armado"|"livre", "classe", "desde", "motivo", "atualizado_em"}.
Armado, a varredura, o executor e a releitura não fazem chamada nova à IA (o
cache válido continua valendo). FALHA FECHADA (revisão da Turing): arquivo
ausente = livre, explicitamente (o PC grava um "livre"); arquivo corrompido ou
ilegível = "ilegivel", que BLOQUEIA como armado; e um armar() que não consegue
gravar mantém o bloqueio em memória, no processo, até ele terminar.
Desarma SÓ pelo comando explícito `python -m painel ia-liberar` (no PC): falta
de saldo ou limite de gasto só se resolve com alguém recarregando ou subindo o
limite, e uma chamada de teste automática a cada rodada gastaria o que resta e
rearmaria o disjuntor de novo.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

CLASSES = ("limite_ou_saldo", "rate_limit", "indeterminado_429", "rede_timeout_5xx", "configuracao",
           "resposta_invalida", "bloqueada")
MENSAGENS = {
    "limite_ou_saldo": "IA sem saldo ou com o limite de gasto atingido",
    "rate_limit": "IA ocupada (limite de chamadas por minuto)",
    "indeterminado_429": "IA recusou a chamada (429 sem motivo reconhecido)",
    "rede_timeout_5xx": "IA fora do ar ou sem resposta (rede, timeout ou erro do servidor)",
    "configuracao": "IA mal configurada (chave, permissão ou modelo)",
    "resposta_invalida": "a IA devolveu uma resposta inválida",
    "bloqueada": "leitura não feita: disjuntor da IA armado",
}
# Classes do DISJUNTOR (o campo "classe" do ia_estado.json) e o motivo gravado.
MOTIVOS_DO_DISJUNTOR = {
    "limite_ou_saldo": MENSAGENS["limite_ou_saldo"],
    "indeterminado": "IA recusou 3 chamadas seguidas com 429 sem motivo reconhecido: pode ser limite ou saldo, "
                     "confira no AI Studio",
}
MOTIVO_ILEGIVEL = "estado da IA ilegível, rode ia-liberar"
SEGUIDOS_PARA_ARMAR_INDETERMINADO = 3

# Só o marcador EXPLÍCITO de "por minuto" é rate limit.
_POR_MINUTO = re.compile(r"per\s*minute|perminute|requests?\s*per\s*min\b", re.I)
# Toda mensagem de cota do Gemini traz "check your plan and billing details",
# inclusive a de 429 por minuto: "billing" sozinho NÃO indica falta de saldo.
# A cota diária também arma: não volta em minutos, e insistir só gera 429.
_SALDO = re.compile(r"spend(?:ing)?\s*cap|prepa(?:y|id)|credit|insufficient|balance|saldo|"
                    r"billing\s+account|billing\s+(?:is\s+)?(?:not\s+enabled|disabled)|enable\s+billing|"
                    r"per\s*day|perday|daily|monthly|per\s*month|permonth", re.I)
_CONFIGURACAO = re.compile(r"api[\s_-]*key|permission_denied|unauthenticated|not\s+found|invalid_argument", re.I)
_SEGREDOS = (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"(?i)(key|token|authorization)[=:]\s*\S+"),
             re.compile(r"(?i)bearer\s+\S+"))


def sanitizar(texto) -> str:
    texto = str(texto or "")
    for padrao in _SEGREDOS:
        texto = padrao.sub("[removido]", texto)
    return " ".join(texto.split())[:400]


class FalhaIA(Exception):
    def __init__(self, classe: str, detalhe: str = ""):
        if classe not in CLASSES:
            raise ValueError(classe)
        self.classe = classe
        self.mensagem = MENSAGENS[classe]
        self.detalhe = sanitizar(detalhe)
        super().__init__(f"{classe}: {self.mensagem}")


def classificar(exc: BaseException) -> FalhaIA:
    """A classe da falha a partir da exceção do SDK (google.genai.errors.APIError
    tem .code e .status), da rede (requests/httpx) ou do sistema operacional."""
    if isinstance(exc, FalhaIA):
        return exc
    codigo = getattr(exc, "code", None)
    if not isinstance(codigo, int):
        codigo = getattr(exc, "status_code", None) if isinstance(getattr(exc, "status_code", None), int) else None
    texto = f"{getattr(exc, 'status', '') or ''} {getattr(exc, 'message', '') or ''} {exc}"
    detalhe = f"{type(exc).__name__} {codigo or ''} {texto}"
    esgotado = codigo == 429 or "RESOURCE_EXHAUSTED" in texto.upper()
    if esgotado and _POR_MINUTO.search(texto):
        return FalhaIA("rate_limit", detalhe)
    if (esgotado or (isinstance(codigo, int) and 400 <= codigo < 500)) and _SALDO.search(texto):
        return FalhaIA("limite_ou_saldo", detalhe)
    if esgotado:
        return FalhaIA("indeterminado_429", detalhe)
    if codigo in (400, 401, 403, 404) or _CONFIGURACAO.search(texto):
        return FalhaIA("configuracao", detalhe)
    return FalhaIA("rede_timeout_5xx", detalhe)  # 5xx, rede, timeout e o que não se reconhece


# ------------------------------------------------------------------ disjuntor
_EM_MEMORIA: dict[str, dict] = {}  # bloqueio de um armar() que não conseguiu gravar (por arquivo)
_trava_da_memoria = threading.Lock()


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _chave(caminho: Path) -> str:
    return os.path.normcase(os.path.abspath(caminho))


def estado(caminho: Path) -> dict:
    """Só lê. Ausente = livre ("desde" None). Corrompido, ilegível ou fora do
    formato = {"estado": "ilegivel", ...}, que bloqueia (falha fechada)."""
    caminho = Path(caminho)
    for tentativa in range(3):
        try:
            bruto = caminho.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return {"estado": "livre", "classe": None, "desde": None, "motivo": "sem registro",
                    "atualizado_em": None}
        except PermissionError:  # Windows: alguém trocando o arquivo neste instante
            if tentativa == 2:
                return _ilegivel()
            time.sleep(0.05)
        except (OSError, ValueError):
            return _ilegivel()
    try:
        dados = json.loads(bruto)
    except ValueError:
        return _ilegivel()
    if not isinstance(dados, dict) or dados.get("estado") not in ("armado", "livre"):
        return _ilegivel()
    return dados


def _ilegivel() -> dict:
    return {"estado": "ilegivel", "classe": None, "desde": None, "motivo": MOTIVO_ILEGIVEL, "atualizado_em": None}


def armado(caminho: Path) -> dict | None:
    """O que bloqueia as chamadas no PC: armado, ilegível ou o bloqueio em
    memória deste processo. Arquivo ausente vira um "livre" gravado."""
    with _trava_da_memoria:
        memoria = _EM_MEMORIA.get(_chave(caminho))
    if memoria:
        return memoria
    e = estado(caminho)
    if e["estado"] == "livre":
        if e["desde"] is None and not Path(caminho).exists():
            agora = _agora()
            try:  # só registro: não gravar o "livre" não bloqueia nem derruba a leitura
                _gravar(caminho, {"estado": "livre", "classe": None, "desde": agora, "motivo": "arquivo novo",
                                  "atualizado_em": agora})
            except Exception:
                pass
        return None
    return e


def _gravar(caminho: Path, dados: dict) -> bool:
    caminho = Path(caminho)
    temporario = caminho.with_name(f".{caminho.name}.{os.getpid()}.{secrets.token_hex(4)}.parcial")
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        for _ in range(40):  # Windows: troca recusada enquanto alguém lê
            try:
                os.replace(temporario, caminho)
                return True
            except PermissionError:
                time.sleep(0.05)
        return False
    except OSError:
        return False
    finally:
        try:
            temporario.unlink(missing_ok=True)
        except OSError:
            pass


def armar(caminho: Path, classe: str) -> bool:
    """Arma (ou mantém armado, sem mudar o 'desde') com uma classe de
    MOTIVOS_DO_DISJUNTOR. True = gravado no arquivo; False = NÃO gravou, e o
    bloqueio fica em memória neste processo até ele terminar (o chamador avisa)."""
    if classe not in MOTIVOS_DO_DISJUNTOR:
        raise ValueError(classe)
    atual = estado(caminho)
    agora = _agora()
    dados = {"estado": "armado", "classe": classe,
             "desde": atual["desde"] if atual["estado"] == "armado" and atual.get("desde") else agora,
             "motivo": MOTIVOS_DO_DISJUNTOR[classe], "atualizado_em": agora}
    if _gravar(caminho, dados):
        return True
    with _trava_da_memoria:
        _EM_MEMORIA.setdefault(_chave(caminho), dict(dados, motivo=dados["motivo"] + " (só em memória: "
                                                                 "não consegui gravar o estado)"))
    return False


def liberar(caminho: Path, por: str = "comando ia-liberar") -> dict:
    """Grava "livre" (também por cima de um estado ilegível) e devolve o anterior."""
    anterior = estado(caminho)
    with _trava_da_memoria:
        _EM_MEMORIA.pop(_chave(caminho), None)
    agora = _agora()
    if not _gravar(caminho, {"estado": "livre", "classe": None, "desde": agora, "motivo": f"liberado por {por}",
                             "atualizado_em": agora}):
        raise OSError(f"não consegui gravar {caminho}")
    return anterior


# ------------------------------------------------------------------ para o site
NOME_DO_ARQUIVO = "ia_estado.json"  # em painel_dados/; no servidor chega pelo envio (opcional)
ARQUIVO_NO_PC = Path(__file__).resolve().parent.parent / "painel_dados" / NOME_DO_ARQUIVO
CAMPOS_DO_ESTADO = ("estado", "classe", "desde", "motivo", "atualizado_em")


def limpar_estado(dados) -> dict | None:
    """O estado como o servidor aceita guardar (vindo do executor ou do envio):
    só os campos conhecidos, texto curto. Qualquer outra coisa = None."""
    if not isinstance(dados, dict) or dados.get("estado") not in ("armado", "livre", "ilegivel"):
        return None
    return {c: (str(dados[c])[:160] if dados.get(c) is not None else None) for c in CAMPOS_DO_ESTADO}


def _momento(texto) -> datetime | None:
    try:
        return datetime.strptime(str(texto or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def para_o_banner(estado_ia: dict | None, visto_em: datetime | None, velho_apos_s: float,
                  agora: datetime | None = None) -> dict | None:
    """O banner do admin: None se a IA está livre (ou sem informação). 'visto_em'
    = quando esse estado foi confirmado pela última vez (no PC: agora; no
    servidor: o último sinal do executor, ou None se só há a cópia do envio, que
    não diz quando foi conferida)."""
    e = limpar_estado(estado_ia)
    if not e or e["estado"] == "livre":
        return None
    agora = agora or datetime.now()
    desde = _momento(e["desde"])
    referencia = visto_em or _momento(e["atualizado_em"])
    return {
        "ilegivel": e["estado"] == "ilegivel",
        "indeterminado": e["classe"] == "indeterminado",
        "desde": (desde.strftime("%H:%M") if desde and desde.date() == agora.date()
                  else desde.strftime("%d/%m %H:%M") if desde else "?"),
        "motivo": e["motivo"],
        "desatualizado": visto_em is None or (agora - visto_em).total_seconds() > velho_apos_s,
        "conferido_em": referencia.strftime("%d/%m %H:%M") if referencia else None,
    }
