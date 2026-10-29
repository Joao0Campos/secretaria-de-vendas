"""
Varredura diária: reanalisa as reservas que estão nas etapas antes da validação
================================================================================

PARA QUE SERVE: o painel (python -m painel) só mostra o que já foi analisado.
Sem rodar todo dia, ele mostra planilha velha - e a reserva no CV já mudou.
Decisão do João (13/10/2026): rodar todo dia às 7h, pelo Agendador de Tarefas
do Windows (varredura_diaria.bat), nos status de STATUS_DA_VARREDURA. Desde
15/10/2026 a tarefa também dispara quando o João entra no Windows - por isso a
rodada é UMA POR DIA: se a de hoje já terminou bem (marcador
painel_dados/ultima_varredura_ok.txt), as seguintes saem sem fazer nada - a não
ser que alguma reserva tenha falhado: aí o disparo seguinte tenta de novo SÓ
essas ("repescagem"), até 3 vezes por reserva no dia.

COMO FUNCIONA:
1. Listagem do CV em cópia LOCAL e enxuta (painel_dados/reservas_cv.json): só
   número, situação, modalidade e data de referência - nenhum nome ou CPF. A
   primeira vez baixa tudo (54 páginas, ~9 min em 13/10/2026); depois pede só o
   que mudou desde a última rodada (a_partir_data_referencia: 1,3 s para um dia,
   medido em 13/10/2026). É o que torna possível rodar todo dia.
2. Para cada reserva nos status da varredura: processar_reserva_completa() +
   processar_pasta_cliente(), o mesmo caminho do testar_amostra.py. Documento já
   lido é reaproveitado (o cache guarda a LEITURA); só documento novo ou trocado
   vai para a IA. A primeira rodada é a cara (~1.200 chamadas); as seguintes,
   só o que mudou.
3. Registro da rodada em painel_dados/varreduras.jsonl (o painel mostra a última)
   e o texto completo em painel_dados/logs/. Tudo em painel_dados/, fora do git.

⚠️ SOMENTE LEITURA no CV. Não altera nada lá.

Como rodar:
    python varredura_diaria.py              -> a varredura do dia (sai se a de
                                               hoje já terminou bem)
    python varredura_diaria.py --forcar     -> roda mesmo que a de hoje já tenha
                                               terminado (a trava continua valendo)
    python varredura_diaria.py --so-listar  -> só atualiza a listagem e mostra
                                               o que seria analisado (sem IA)
    python varredura_diaria.py --atualizar-funil
                                            -> SÓ a listagem, sem análise e sem
                                               IA (Agendador, a cada 15 min:
                                               atualizar_funil.bat). Trava ocupada:
                                               sai calado com 0. Uma linha no log.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import secrets
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mvp_v1_validador import base_url, carregar_config, headers_v1, requisicao_com_retry
from mvp_v2_reserva import (
    DocumentosIndisponiveisNoCV,
    buscar_reserva_individual,
    listar_todas_reservas,
    processar_reserva_completa,
)
from analisar_documentos import processar_pasta_cliente

RAIZ = Path(__file__).resolve().parent
PASTA_DADOS = RAIZ / "painel_dados"
ARQ_LISTAGEM = PASTA_DADOS / "reservas_cv.json"
ARQ_REGISTRO = PASTA_DADOS / "varreduras.jsonl"
ARQ_TRAVA = PASTA_DADOS / "varredura.trava"
ARQ_ULTIMA_OK = PASTA_DADOS / "ultima_varredura_ok.txt"
PASTA_LOGS = PASTA_DADOS / "logs"

# As etapas ANTES da validação, mais a própria fila de validação: pegar o
# problema antes de a venda chegar na mesa de quem valida. Escolha do João,
# 13/10/2026 (na data: 20 + 19 + 5 + 4 + 3 = 51 reservas). Os nomes são os do
# CV, comparados sem espaço nas pontas ("Aguardando Assinaturas/Impressão
# Boleto " vem com espaço no fim).
STATUS_DA_VARREDURA = [
    "Contrato Aguardando Validação",
    "Nova Reserva",
    "Reserva Aprovada",
    "Análise Gerencial",
    "Geração de Contrato",
]

# Campos guardados da listagem. Nada de cliente, CPF, e-mail: a cópia local
# serve só para saber em que etapa cada reserva está.
CAMPOS_GUARDADOS = ("idreserva", "idprecadastro", "situacao", "tipovenda",
                    "referencia_data", "data_ultima_alteracao_situacao")

# A busca incremental volta um pouco antes da última data vista: alteração
# gravada com atraso no CVDW não pode escapar.
MARGEM_INCREMENTAL = timedelta(days=2)

# Prazo máximo de uma trava cujo dono não dá para PROVAR vivo e o mesmo
# processo (ver _trava_orfa()): passado isto, ela é abandonada. Trava nova de
# processo provadamente morto é recuperada na hora; a do formato antigo (só o
# PID) espera este prazo. Eram 6 h; com o disparo também no logon (15/10/2026), 3 h.
TRAVA_VELHA = timedelta(hours=3)

# Quem segura a trava, gravado nela ("modo"). A varredura e a repescagem ESPERAM
# o funil soltar (19/10/2026): ele dura segundos (a listagem incremental), e a
# tarefa dele dispara a cada 15 min a partir das 7h - às 7h em ponto ele pode
# estar com a trava. Sem esperar, a varredura das 7h saía com "outra varredura
# está rodando", sem marcador, e o dia só voltava a ter chance no próximo logon.
# Trava de OUTRA varredura (ou sem modo, das versões anteriores) não é esperada.
# A reanálise sob demanda (executor_reanalise.py, 19/10/2026) também é esperada:
# o executor atende UM pedido por vez e solta a trava por 15 s entre um e outro,
# então a varredura (que confere a cada 10 s) entra na primeira folga - a espera
# é de no máximo UMA reanálise, nunca da fila inteira.
# "releitura" (--reler-telas, 19/10/2026) pega a trava uma reserva por vez: curta.
MODOS_QUE_A_VARREDURA_ESPERA = ("funil", "reanalise", "releitura")
# Até quanto esperar. Pior caso do funil: a listagem COMPLETA (sem
# reservas_cv.json), ~9 min medidos em 13/10/2026. Pior caso de UMA reanálise:
# a média medida é ~1 min por reserva (29 min para 33 em 19/10/2026), e uma
# reserva nova com ~20 documentos para a IA (5 em paralelo) leva poucos minutos;
# só passaria disto com o CV respondendo 429 em quase toda requisição.
ESPERA_PELA_TRAVA = timedelta(minutes=15)
INTERVALO_DA_ESPERA_S = 10
# Quantas vezes tentar de novo, na hora, quando a trava some entre a tentativa e
# a leitura do dono (ver _pegar_trava_esperando()).
TENTATIVAS_SE_A_TRAVA_SUMIR = 5


class Saida:
    """Escreve no terminal e no arquivo de log do dia ao mesmo tempo."""

    def __init__(self, *destinos):
        self.destinos = destinos

    def write(self, texto):
        for d in self.destinos:
            try:
                d.write(texto)
            except UnicodeEncodeError:
                d.write(texto.encode("ascii", "replace").decode("ascii"))
            # Linha a linha: quem abre o log no meio da rodada vê onde ela está.
            if "\n" in texto:
                d.flush()
        return len(texto)

    def flush(self):
        for d in self.destinos:
            d.flush()


# ------------------------------------------------------------------ listagem
def _enxugar(item: dict) -> dict:
    return {k: item.get(k) for k in CAMPOS_GUARDADOS}


def _ler_listagem() -> dict:
    if not ARQ_LISTAGEM.exists():
        return {}
    try:
        return json.loads(ARQ_LISTAGEM.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Cópia corrompida: baixa tudo de novo, é só uma rodada mais lenta.
        return {}


def _gravar_listagem(dados: dict) -> None:
    temporario = ARQ_LISTAGEM.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    temporario.replace(ARQ_LISTAGEM)


def _buscar_alteradas_desde(cfg: dict, desde: str) -> list[dict]:
    url = f"{base_url(cfg)}/v1/cvdw/reservas"
    pagina, itens = 1, []
    while True:
        resp = requisicao_com_retry(
            url, headers=headers_v1(cfg),
            params={"pagina": pagina, "registros_por_pagina": 500, "a_partir_data_referencia": desde},
            contexto=f"reservas alteradas desde {desde}, página {pagina}")
        if resp.status_code != 200:
            raise SystemExit(f"Erro {resp.status_code} na listagem incremental (página {pagina})")
        corpo = resp.json()
        lote = corpo.get("dados") or []
        itens.extend(lote)
        if pagina >= (corpo.get("total_de_paginas") or 1) or not lote:
            return itens
        pagina += 1


def atualizar_listagem(cfg: dict) -> tuple[dict, str]:
    """Devolve {idreserva: registro enxuto} e como foi obtida.

    ⚠️ A listagem vem do CVDW, que ATRASA - às vezes horas. Em 13/10/2026, 9
    reservas que ela dava como "Nova Reserva" já estavam canceladas desde 09-10/10.
    O que ela diz é "segundo a listagem do CV"; a situação real de uma reserva só
    vale depois da busca direta, que _analisar_reserva() faz antes de analisar."""
    atual = _ler_listagem()
    reservas: dict[str, dict] = atual.get("reservas") or {}
    ultima = atual.get("ultima_referencia")

    if not reservas or not ultima:
        print("Listagem local não existe: baixando todas as reservas (só desta vez, ~9 min)...")
        with open(os.devnull, "w") as nulo, contextlib.redirect_stdout(nulo):
            todas = listar_todas_reservas(cfg)
        reservas = {str(i["idreserva"]): _enxugar(i) for i in todas if i.get("idreserva")}
        como = f"completa ({len(reservas)} reservas)"
    else:
        desde_dt = datetime.strptime(ultima[:19], "%Y-%m-%d %H:%M:%S") - MARGEM_INCREMENTAL
        desde = desde_dt.strftime("%Y-%m-%d %H:%M:%S")
        alteradas = _buscar_alteradas_desde(cfg, desde)
        for i in alteradas:
            if i.get("idreserva"):
                reservas[str(i["idreserva"])] = _enxugar(i)
        como = f"incremental desde {desde} ({len(alteradas)} alteradas)"

    datas = [r.get("referencia_data") for r in reservas.values() if r.get("referencia_data")]
    _gravar_listagem({
        "atualizado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ultima_referencia": max(datas) if datas else ultima,
        "reservas": reservas,
    })
    return reservas, como


def situacao_atual_no_cv(cfg: dict, idreserva: int) -> str | None:
    """Situação da reserva pela busca direta (o CV de verdade, sem o atraso do
    CVDW). None se a busca não responder - aí vale a listagem."""
    try:
        with open(os.devnull, "w") as nulo, contextlib.redirect_stdout(nulo):
            registro = buscar_reserva_individual(cfg, idreserva)
    except Exception:
        return None
    situacao = (registro or {}).get("situacao")
    if isinstance(situacao, dict):
        situacao = situacao.get("situacao")
    return situacao if isinstance(situacao, str) and situacao.strip() else None


def selecionar(reservas: dict) -> list[dict]:
    alvo = {s.strip().lower() for s in STATUS_DA_VARREDURA}
    escolhidas = [r for r in reservas.values() if (r.get("situacao") or "").strip().lower() in alvo]
    # As mais recentes primeiro: se a rodada cair no meio, o que entrou por
    # último no funil já foi visto.
    escolhidas.sort(key=lambda r: int(r.get("idreserva") or 0), reverse=True)
    return escolhidas


# ------------------------------------------------------------------ rodada
def _registrar(rodada: dict) -> None:
    # O marcador do dia e esta linha são gravados separados, sem journal
    # (decisão da 3ª revisão da Forja, 15/10/2026): se esta falhar, o dia
    # continua feito e o painel só não mostra a rodada. Fica no log.
    try:
        with ARQ_REGISTRO.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rodada, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"ATENÇÃO: não consegui gravar {ARQ_REGISTRO.name} ({e}) - a rodada não "
              f"aparece no painel, mas o marcador do dia vale.")


# ----------------------------------------------------- marcador do dia
# painel_dados/ultima_varredura_ok.txt, gravado SÓ no fim de uma rodada que
# passou por todas as reservas. Desde 15/10/2026 (2ª revisão da Forja) é um
# JSON com o resumo da rodada e as reservas que falharam ("pendentes"), para o
# próximo disparo do mesmo dia (logon) tentar de novo SÓ elas. O formato antigo
# - só "AAAA-MM-DD HH:MM:SS", gravado pela rodada de 15/10/2026 de manhã -
# continua sendo lido: os pendentes saem das "falhas" da última rodada NORMAL
# daquele dia em varreduras.jsonl (3ª revisão da Forja), com 1 tentativa cada.


def _ultima_rodada_normal_do_dia(dia: str) -> dict | None:
    """A última linha de rodada completa ("ok"/"ok_com_falhas", não repescagem)
    de varreduras.jsonl começada no dia 'AAAA-MM-DD'."""
    try:
        linhas = ARQ_REGISTRO.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for linha in reversed(linhas):
        try:
            r = json.loads(linha)
        except ValueError:
            continue
        if (isinstance(r, dict) and not r.get("tipo") and str(r.get("inicio", ""))[:10] == dia
                and r.get("resultado") in ("ok", "ok_com_falhas")):
            return r
    return None

def _ler_marcador() -> dict | None:
    try:
        texto = ARQ_ULTIMA_OK.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        marcador = json.loads(texto)
        datetime.strptime(marcador["concluida_em"], "%Y-%m-%d %H:%M:%S")
        marcador.setdefault("pendentes", {})
        return marcador
    except (ValueError, KeyError, TypeError):
        pass
    try:  # formato de 15/10/2026 de manhã
        datetime.strptime(texto[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    marcador = {"concluida_em": texto[:19], "pendentes": {}}
    rodada = _ultima_rodada_normal_do_dia(texto[:10])
    if rodada:
        marcador.update(
            reservas_alvo=rodada.get("reservas_alvo", 0), reservas_ok=rodada.get("reservas_ok", 0),
            pastas=rodada.get("pastas", 0),
            pendentes={str(f["idreserva"]): _pendente(f)
                       for f in rodada.get("falhas") or [] if isinstance(f, dict) and f.get("idreserva")})
    return marcador


def _ultima_ok() -> datetime | None:
    """Quando terminou a última rodada bem-sucedida. None se nunca houve, ou
    se o marcador não se lê."""
    marcador = _ler_marcador()
    return datetime.strptime(marcador["concluida_em"], "%Y-%m-%d %H:%M:%S") if marcador else None


def _gravar_marcador(marcador: dict) -> None:
    # Temporário EXCLUSIVO deste processo, depois troca: dois processos nunca
    # escrevem no mesmo .tmp, e quem lê nunca vê um marcador pela metade.
    temporario = ARQ_ULTIMA_OK.with_name(
        f"{ARQ_ULTIMA_OK.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporario.write_text(json.dumps(marcador, ensure_ascii=False), encoding="utf-8")
    temporario.replace(ARQ_ULTIMA_OK)


def _pendente(falha: dict) -> dict:
    """Falha da rodada -> pendente da repescagem: 1 tentativa feita; a
    'permanente' (o CV não entrega os documentos) já sai sem nova tentativa hoje."""
    pendente = {"tentativas": TENTATIVAS_POR_DIA if falha.get("permanente") else 1,
                "erro": falha.get("erro", "")}
    if falha.get("permanente"):
        pendente["permanente"] = True
    return pendente


def _marcar_ok(momento: datetime, rodada: dict | None = None) -> None:
    """Marcador de uma rodada completa. Cada falha começa com 1 tentativa
    (a permanente, já esgotada - ver _pendente)."""
    rodada = rodada or {}
    _gravar_marcador({
        "concluida_em": momento.strftime("%Y-%m-%d %H:%M:%S"),
        "reservas_alvo": rodada.get("reservas_alvo", 0),
        "reservas_ok": rodada.get("reservas_ok", 0),
        "pastas": rodada.get("pastas", 0),
        "pendentes": {str(f["idreserva"]): _pendente(f) for f in rodada.get("falhas", [])},
    })


# ----------------------------------------------------------------- trava
# Trava entre processos (revisões da Forja de 15/10/2026). A tarefa dispara às
# 7h E no logon, e o João também roda na mão: dois processos podem chegar
# juntos. "exists() e depois write_text()" deixava os dois passarem. Agora:
#   - criação ATÔMICA (os.open com O_CREAT | O_EXCL): só um consegue criar;
#   - o arquivo guarda PID, token e início (JSON);
#   - trava cujo dono está provadamente MORTO (ou cujo PID foi reaproveitado
#     por um processo mais novo que a trava) é recuperada na hora;
#   - quando não dá para PROVAR que o dono está vivo e é o mesmo processo
#     (acesso negado, horário de criação ilegível, trava sem 'inicio'), vale
#     o prazo de TRAVA_VELHA contado do 'inicio' (ou do mtime): nenhuma trava
#     bloqueia mais que isso sem essa prova;
#   - trava do formato ANTIGO (só o PID, sem token - a da rodada de 15/10/2026
#     de manhã), vazia ou ilegível NÃO passa pela checagem de PID: fica ocupada
#     até o arquivo ter TRAVA_VELHA;
#   - recuperar = renomear para um nome só deste processo e conferir que o que
#     saiu tem o MESMO conteúdo e o MESMO mtime que foram julgados; se não
#     tiver (outro processo recuperou e já criou a dele), devolve e desiste;
#   - no fim, só apaga a trava quem ainda é o dono (mesmo PID e token).
# Resta uma janela teórica: se, entre tirar a trava errada e devolvê-la, um
# TERCEIRO processo criar a dele, a devolução falha e duas rodadas seguem. São
# três processos no mesmo milissegundo; o log registra quando acontece.

def _foto_da_trava() -> tuple[bytes, int] | None:
    """(conteúdo, mtime em ns) da trava - o que é julgado e depois conferido.
    None se ela não existe mais."""
    try:
        dados = ARQ_TRAVA.read_bytes()
        return dados, ARQ_TRAVA.stat().st_mtime_ns
    except OSError:
        return None


def _interpretar_trava(dados: bytes) -> dict:
    """{"pid", "token", "inicio"} da trava nova; {"pid", "antiga": True} da
    trava antiga (só o PID - que json.loads leria como número); {} se vazia ou
    ilegível."""
    texto = dados.decode("utf-8", errors="replace").strip()
    if texto.isdigit():
        return {"pid": int(texto), "antiga": True}
    try:
        info = json.loads(texto)
    except ValueError:
        return {}
    return info if isinstance(info, dict) else {}


def _ler_trava() -> dict | None:
    foto = _foto_da_trava()
    return _interpretar_trava(foto[0]) if foto else None


def _processo_vivo(pid: int, desde: datetime | None = None) -> bool | None:
    """True só quando dá para PROVAR que o processo 'pid' está vivo E é o dono
    da trava (criado antes de 'desde', o início dela). False quando ele
    provadamente não está: não existe, já saiu, ou é um processo MAIS NOVO que
    a trava (PID reaproveitado). None quando não dá para saber - acesso negado,
    horário de criação ilegível, sem 'desde' para comparar."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)  # sinal 0: só pergunta, não mata (POSIX)
        except ProcessLookupError:
            return False
        except OSError:
            return None
        return None  # vivo, mas sem como provar que é o mesmo processo
    # Windows: os.kill(pid, 0) MATARIA o processo - aqui é OpenProcess.
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # 87 = ERROR_INVALID_PARAMETER: não existe processo com esse PID.
        # 5 (acesso negado) e o resto: desconhecido - vale o prazo.
        return False if ctypes.get_last_error() == 87 else None
    try:
        codigo = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
            return None
        if codigo.value != 259:  # STILL_ACTIVE
            return False
        if desde is None:
            return None
        criado, saida, kernel, usuario = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(criado), ctypes.byref(saida),
                                   ctypes.byref(kernel), ctypes.byref(usuario)):
            return None
        ticks = (criado.dwHighDateTime << 32) | criado.dwLowDateTime  # 100 ns desde 1601
        criado_em = datetime(1601, 1, 1) + timedelta(microseconds=ticks // 10)
        criado_em = criado_em.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        if criado_em > desde + timedelta(seconds=2):
            return False  # PID reaproveitado por um processo mais novo que a trava
        return True
    finally:
        k32.CloseHandle(handle)


def _trava_orfa(dados: bytes, mtime_ns: int) -> str | None:
    """Motivo para considerar a trava abandonada, ou None se ela vale."""
    info = _interpretar_trava(dados)
    agora = datetime.now()
    pelo_arquivo = agora - datetime.fromtimestamp(mtime_ns / 1e9)
    pid = info.get("pid")
    if info.get("token") and isinstance(pid, int):
        desde = None
        try:
            desde = datetime.strptime(str(info.get("inicio")), "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            pass
        vivo = _processo_vivo(pid, desde)
        if vivo is False:
            return f"o processo {pid} que a criou não está mais rodando"
        if vivo:
            return None
        idade = agora - desde if desde else pelo_arquivo
        if idade >= TRAVA_VELHA:
            return f"tem {idade} e não dá para provar que o processo {pid} ainda é o dono"
        return None
    # Formato antigo, vazia ou ilegível: só o prazo, pelo arquivo.
    if pelo_arquivo >= TRAVA_VELHA:
        return f"trava sem token com {pelo_arquivo}"
    return None


def _tirar_se_igual(foto: tuple[bytes, int], token: str) -> str:
    """Tira a trava julgada órfã do caminho, SE ainda for ela. Devolve
    "tirada", "sumiu" (alguém já tirou - é só tentar criar de novo) ou
    "mudou" (outro processo já recuperou e criou a dele: desiste).

    No Windows o rename falha (PermissionError) enquanto OUTRO processo está
    lendo a trava para julgá-la - com vários disputando, todos se bloqueavam e
    ninguém rodava (2 de 20 disputas no teste de estresse de 15/10/2026). Isso
    é passageiro: tenta de novo por até ~1 s, conferindo a foto a cada vez."""
    parada = ARQ_TRAVA.with_name(f"{ARQ_TRAVA.name}.orfa.{os.getpid()}.{token}")
    for tentativa in range(40):
        atual = _foto_da_trava()
        if atual is None:
            return "sumiu"
        if atual != foto:
            return "mudou"
        try:
            os.rename(ARQ_TRAVA, parada)
            break
        except FileNotFoundError:
            return "sumiu"
        except PermissionError:
            time.sleep(0.01 + 0.02 * random.random())
        except OSError:
            return "mudou"
    else:
        return "mudou"
    # Ler a trava estacionada também pode bater em PermissionError passageiro
    # (antivírus/indexador abrindo o arquivo): sem repetir, a órfã de verdade
    # era tratada como "mudou", não voltava (já havia trava nova) e ficava
    # esquecida como .orfa (teste de disputa de 19/10/2026).
    igual = False
    for _ in range(40):
        try:
            igual = (parada.read_bytes(), parada.stat().st_mtime_ns) == foto
            break
        except PermissionError:
            time.sleep(0.01 + 0.02 * random.random())
        except OSError:
            break
    if igual:
        for _ in range(40):
            try:
                parada.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.01 + 0.02 * random.random())
        return "tirada"
    try:
        os.rename(parada, ARQ_TRAVA)  # devolve a trava de quem recuperou antes
    except OSError:
        print(f"ATENÇÃO: não consegui devolver a trava de outra rodada ({parada.name}); "
              f"pode haver duas varreduras ao mesmo tempo.")
    return "mudou"


def _pegar_trava(modo: str = "varredura") -> str | None:
    """Tenta criar a trava. Devolve o token do dono, ou None se outra rodada
    está viva. 'modo' (varredura, repescagem, funil) diz a quem chega depois
    quem está com ela - ver MODOS_QUE_A_VARREDURA_ESPERA."""
    token = secrets.token_hex(8)
    conteudo = json.dumps({"pid": os.getpid(), "token": token, "modo": modo,
                           "inicio": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")})
    for _ in range(20):
        try:
            fd = os.open(ARQ_TRAVA, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except PermissionError:
            # Windows: a trava está sendo apagada/renomeada agora por outro
            # processo ("delete pending"). Passageiro - tenta de novo.
            time.sleep(0.01 + 0.02 * random.random())
            continue
        except FileExistsError:
            foto = _foto_da_trava()
            if foto is None:
                continue
            motivo = _trava_orfa(*foto)
            if motivo is None:
                return None
            resultado = _tirar_se_igual(foto, token)
            if resultado == "mudou":
                return None
            if resultado == "tirada":
                print(f"Trava órfã recuperada: {motivo}.")
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conteudo)
        return token
    return None


def _pegar_trava_esperando(modo: str) -> tuple[str | None, bool]:
    """_pegar_trava(), mas se quem está com a trava é um trabalho curto
    (MODOS_QUE_A_VARREDURA_ESPERA - hoje, o funil), espera ele soltar, até
    ESPERA_PELA_TRAVA. Devolve (token, se esperou); token None se a trava é de
    outra varredura, ou se o prazo acabou."""
    limite = datetime.now() + ESPERA_PELA_TRAVA
    avisou = False
    sumiu = 0
    while True:
        token = _pegar_trava(modo)
        if token is not None:
            return token, avisou
        info = _ler_trava()
        if not info:
            # A trava sumiu entre a tentativa e a leitura (o funil acabou de
            # soltar: None), ou alguém acabou de criá-la e ainda não escreveu
            # ({}). Isso não prova que há outra varredura: tenta de novo NA
            # HORA, sem contar como espera (revisão da Forja, 19/10/2026 - sem
            # isto a varredura saía com a trava livre). O limite evita laço
            # infinito; esgotado, a trava vazia conta como "sem modo".
            sumiu += 1
            if sumiu <= TENTATIVAS_SE_A_TRAVA_SUMIR:
                if info is not None:
                    time.sleep(0.05)
                continue
            return None, avisou
        sumiu = 0
        dono = info.get("modo")
        if dono not in MODOS_QUE_A_VARREDURA_ESPERA or datetime.now() >= limite:
            return None, avisou
        if not avisou:
            print(f"A trava está com o {dono} - esperando ele terminar "
                  f"(até {ESPERA_PELA_TRAVA.seconds // 60} min).")
            avisou = True
        time.sleep(INTERVALO_DA_ESPERA_S)


def _soltar_trava(token: str) -> None:
    info = _ler_trava() or {}
    if info.get("token") == token and info.get("pid") == os.getpid():
        ARQ_TRAVA.unlink(missing_ok=True)
    else:
        print("A trava não é mais desta rodada (outro processo a recuperou) - não foi apagada.")


# ------------------------------------------------------------ uma reserva
def _analisar_reserva(cfg: dict, idreserva: int, situacao_listagem: str, rodada: dict) -> str:
    """Analisa UMA reserva e anota em 'rodada'. Devolve "ok", "fora" (saiu do
    funil) ou "falhou"."""
    status_alvo = {s.strip().lower() for s in STATUS_DA_VARREDURA}
    # O CVDW (de onde vem a listagem) atrasa: na 1ª rodada (13/10/2026)
    # 9 reservas que ele dava como "Nova Reserva" já estavam canceladas
    # desde 09-10/10, e 4 já tinham avançado de etapa. A busca direta
    # (/v1/comercial/reservas/{id}) é o CV de verdade e custa 0,75 s:
    # confirma antes de gastar IA. Se ela falhar, segue com a listagem.
    atual = situacao_atual_no_cv(cfg, idreserva)
    if atual and atual.strip().lower() not in status_alvo:
        print(f"  pulada: hoje no CV está em '{atual.strip()}' (a listagem ainda dizia "
              f"'{situacao_listagem}')")
        rodada.setdefault("fora_do_funil", []).append({"idreserva": idreserva, "situacao": atual.strip()})
        return "fora"
    try:
        pastas, associados, falha = processar_reserva_completa(idreserva, devolver_associados=True)
        for pasta in pastas:
            processar_pasta_cliente(pasta, associados_consultados=associados, falha_associados=falha)
        rodada["reservas_ok"] += 1
        rodada["pastas"] += len(pastas)
        return "ok"
    except (Exception, SystemExit) as e:  # uma reserva quebrada não derruba a rodada
        print(f"  FALHOU: {str(e)[:300]}")
        falha = {"idreserva": idreserva, "erro": str(e).split(":", 1)[0][:120]}
        if isinstance(e, DocumentosIndisponiveisNoCV):
            falha["erro"] = e.motivo[:160]
            if e.permanente:  # arquivo ausente confirmado no CV: não repetir hoje
                falha["permanente"] = True
        rodada["falhas"].append(falha)
        return "falhou"


# ------------------------------------------------------------ repescagem
# Decisão do João (15/10/2026): rodada que termina "ok_com_falhas" conta o dia
# como feito, mas o próximo disparo do mesmo dia tenta de novo SÓ as reservas
# que falharam - no máximo TENTATIVAS_POR_DIA vezes cada. A falha 'permanente'
# (o CV não entrega os documentos: 422 de 34441/34442, ver
# mvp_v2_reserva.DocumentosIndisponiveisNoCV) não é repetida no dia (20/10/2026).
TENTATIVAS_POR_DIA = 3


def _repescagem(marcador: dict, inicio: datetime) -> int:
    _marcar_origem_do_consumo()
    pendentes = marcador["pendentes"]
    da_vez = sorted((r for r, p in pendentes.items() if p.get("tentativas", 1) < TENTATIVAS_POR_DIA), key=int)
    rodada = {"inicio": inicio.strftime("%Y-%m-%d %H:%M:%S"), "tipo": "repescagem",
              "status_da_varredura": STATUS_DA_VARREDURA, "resultado": "erro",
              "reservas_alvo": marcador.get("reservas_alvo", 0), "reservas_ok": marcador.get("reservas_ok", 0),
              "pastas": marcador.get("pastas", 0), "falhas": [],
              "repescagem": {"tentadas": [int(r) for r in da_vez], "recuperadas": [], "fora_do_funil": []}}
    print(f"repescagem: {len(da_vez)} reserva(s) que falharam hoje ({', '.join(da_vez)})")
    token, _ = _pegar_trava_esperando("repescagem")
    if token is None:
        print("Outra varredura está rodando agora. Saindo sem fazer nada.")
        return 1
    try:
        cfg = carregar_config()
        for i, idreserva in enumerate(da_vez, start=1):
            tentativa = pendentes[idreserva].get("tentativas", 1) + 1
            print(f"\n[repescagem {i}/{len(da_vez)}] reserva {idreserva} (tentativa {tentativa} de "
                  f"{TENTATIVAS_POR_DIA} hoje)")
            pendentes[idreserva]["tentativas"] = tentativa
            parcial = {"reservas_ok": 0, "pastas": 0, "falhas": []}
            resultado = _analisar_reserva(cfg, int(idreserva), "falhou na rodada anterior", parcial)
            if resultado == "falhou":
                pendentes[idreserva] = dict(_pendente(parcial["falhas"][0]),
                                            tentativas=max(tentativa, _pendente(parcial["falhas"][0])["tentativas"]))
                continue
            del pendentes[idreserva]
            if resultado == "ok":
                rodada["repescagem"]["recuperadas"].append(int(idreserva))
                rodada["reservas_ok"] += 1
                rodada["pastas"] += parcial["pastas"]
            else:
                rodada["repescagem"]["fora_do_funil"].append(int(idreserva))
        return 0
    except (Exception, SystemExit) as e:
        print("A repescagem parou:")
        traceback.print_exc()
        rodada["erro"] = str(e).split(":", 1)[0][:200]
        return 2
    finally:
        # O registro mostra o DIA, não só a repescagem: o painel lê a última
        # linha de varreduras.jsonl ("ok/alvo" e falhas no topo), e uma linha
        # "0/2" esconderia que a rodada das 7h passou por 28/30. Saída "fora
        # do funil" não conta como ok nem como falha.
        rodada["falhas"] = [dict({"idreserva": int(r), "erro": p.get("erro", "")},
                                 **({"permanente": True} if p.get("permanente") else {}))
                            for r, p in sorted(pendentes.items(), key=lambda x: int(x[0]))]
        # Repescagem que parou no meio não desfaz o dia (já feito às 7h): segue
        # "ok_com_falhas", com o motivo em "erro".
        rodada["resultado"] = "ok" if not rodada["falhas"] and "erro" not in rodada else "ok_com_falhas"
        marcador["reservas_ok"] = rodada["reservas_ok"]
        marcador["pastas"] = rodada["pastas"]
        marcador["repescagem_em"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _gravar_marcador(marcador)
        fim = datetime.now()
        rodada["fim"] = fim.strftime("%Y-%m-%d %H:%M:%S")
        rodada["duracao_s"] = round((fim - inicio).total_seconds())
        _registrar(rodada)
        _soltar_trava(token)
        recuperadas = rodada["repescagem"]["recuperadas"]
        print(f"\nRepescagem: {len(recuperadas)}/{len(da_vez)} recuperada(s) | dia: "
              f"{rodada['reservas_ok']}/{rodada['reservas_alvo']} reservas, "
              f"{len(rodada['falhas'])} ainda com falha" + _linha_do_consumo(inicio))


def _linha_do_consumo(desde: datetime) -> str:
    """Tokens e custo estimado da IA nesta rodada (painel_dados/consumo_ia.jsonl)."""
    try:
        import analisar_documentos as ad
        from painel import consumo
        r = consumo.resumo_desde(ad.PASTA_CONSUMO_IA, desde, origem="varredura")
        tokens = r["entrada"] + r["saida"] + r["raciocinio"]
        mil = lambda n: f"{n:,}".replace(",", ".")  # noqa: E731
        custo = (f"~US$ {r['usd']:.4f} (R$ {r['brl']:.2f})" if r["usd"] is not None
                 else f"custo desconhecido (modelo sem preço: {', '.join(r['modelos_sem_preco'])})")
        return (f" | IA: {r['chamadas']} leitura(s) + {r['do_cache']} do cache, {mil(tokens)} tokens "
                f"({mil(r['entrada'])} entrada, {mil(r['saida'])} saída, {mil(r['raciocinio'])} raciocínio), "
                + custo)
    except Exception:  # medir nunca derruba a varredura
        return ""


def _marcar_origem_do_consumo() -> None:
    try:
        import analisar_documentos as ad
        ad.ORIGEM_DO_CONSUMO = "varredura"
    except Exception:
        pass


def rodar(so_listar: bool = False, forcar: bool = False) -> int:
    inicio = datetime.now()
    _marcar_origem_do_consumo()
    rodada = {"inicio": inicio.strftime("%Y-%m-%d %H:%M:%S"), "status_da_varredura": STATUS_DA_VARREDURA,
              "resultado": "erro", "reservas_alvo": 0, "reservas_ok": 0, "pastas": 0, "falhas": []}
    # Uma rodada por dia (15/10/2026): a tarefa dispara às 7h E no logon. Só o
    # marcador de SUCESSO conta - rodada que caiu no meio ou parou com erro não
    # grava marcador, e a próxima disparada roda de novo. A saída "já rodou"
    # não entra em varreduras.jsonl: o painel mostra a última rodada de verdade.
    marcador = None if (forcar or so_listar) else _ler_marcador()
    if marcador and marcador["concluida_em"][:10] == inicio.strftime("%Y-%m-%d"):
        da_vez = [r for r, p in marcador["pendentes"].items()
                  if p.get("tentativas", 1) < TENTATIVAS_POR_DIA]
        if da_vez:
            return _repescagem(marcador, inicio)
        esgotadas = sorted(marcador["pendentes"], key=int)
        print(f"Varredura já rodou hoje às {marcador['concluida_em'][11:16]} - nada a fazer "
              f"(--forcar roda de novo)."
              + (f" Sem nova tentativa hoje (limite de {TENTATIVAS_POR_DIA}): "
                 f"{', '.join(esgotadas)}." if esgotadas else ""))
        return 0
    token, esperou = _pegar_trava_esperando("varredura")
    if token is None:
        print("Outra varredura está rodando agora. Saindo sem fazer nada.")
        return 1
    if esperou and not (forcar or so_listar):
        # Esperou o funil: confere de novo se outro disparo não fez o dia
        # enquanto isso. Se fez, decide como se tivesse chegado agora.
        depois = _ler_marcador()
        if depois and depois["concluida_em"][:10] == inicio.strftime("%Y-%m-%d"):
            _soltar_trava(token)
            return rodar(so_listar=so_listar, forcar=forcar)
    try:
        cfg = carregar_config()
        reservas, como = atualizar_listagem(cfg)
        rodada["listagem"] = como
        alvo = selecionar(reservas)
        rodada["reservas_alvo"] = len(alvo)
        print(f"Listagem {como}.")
        print(f"{len(alvo)} reserva(s) nos status da varredura:")
        for s in STATUS_DA_VARREDURA:
            n = sum(1 for r in alvo if (r.get("situacao") or "").strip().lower() == s.lower())
            print(f"  {n:4d}  {s}")
        if so_listar:
            rodada["resultado"] = "so_listagem"
            return 0

        rodada["fora_do_funil"] = []
        for i, item in enumerate(alvo, start=1):
            idreserva = int(item["idreserva"])
            situacao = (item.get("situacao") or "").strip()
            print(f"\n[{i}/{len(alvo)}] reserva {idreserva} ({situacao})")
            if _analisar_reserva(cfg, idreserva, situacao, rodada) == "fora":
                reservas[str(idreserva)]["situacao"] = rodada["fora_do_funil"][-1]["situacao"]
        rodada["resultado"] = "ok" if not rodada["falhas"] else "ok_com_falhas"
        if rodada["fora_do_funil"]:
            # Corrige a cópia local com o que a busca direta viu, para a
            # rodada de amanhã nem considerar essas reservas.
            dados = _ler_listagem()
            dados["reservas"] = reservas
            _gravar_listagem(dados)
        # A rodada passou por todas as reservas: o dia está feito. As que
        # falharam vão no marcador para a repescagem (ver _repescagem()).
        _marcar_ok(datetime.now(), rodada)
        return 0
    except (Exception, SystemExit) as e:
        print("A varredura parou:")
        traceback.print_exc()
        rodada["erro"] = str(e).split(":", 1)[0][:200]
        return 2
    finally:
        fim = datetime.now()
        rodada["fim"] = fim.strftime("%Y-%m-%d %H:%M:%S")
        rodada["duracao_s"] = round((fim - inicio).total_seconds())
        _registrar(rodada)
        _soltar_trava(token)
        print(f"\nResultado: {rodada['resultado']} | {rodada['reservas_ok']}/{rodada['reservas_alvo']} reservas, "
              f"{rodada['pastas']} pastas, {len(rodada['falhas'])} falha(s), {rodada['duracao_s']} s"
              + _linha_do_consumo(inicio))


# ------------------------------------------------------------------ funil
def _linha_no_log(texto: str) -> None:
    """Uma linha no log do dia, sem o cabeçalho "=====" de cada rodada: o funil
    roda ~48 vezes por dia."""
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    with (PASTA_LOGS / f"varredura-{datetime.now():%Y-%m-%d}.txt").open("a", encoding="utf-8") as log:
        log.write(f"{datetime.now():%H:%M:%S} {texto}\n")


def atualizar_funil() -> int:
    """--atualizar-funil (decisão do João, 19/10/2026): SÓ atualizar_listagem(),
    a cada 15 min no horário comercial, para o painel mostrar o funil do CV quase
    em tempo real. Nada de análise nem IA.

    - Trava ocupada (varredura, repescagem ou outro funil): sai CALADO com 0 - a
      próxima execução, 15 min depois, tenta de novo. Usa a MESMA trava, porque
      reservas_cv.json não pode ser gravado por dois ao mesmo tempo.
    - Não grava em varreduras.jsonl (o painel usa para "última varredura"; 48
      linhas por dia o poluiriam) e não toca no marcador do dia nem na
      repescagem: o 'atualizado_em' de reservas_cv.json já diz quando o funil
      foi atualizado.
    - Uma linha no log do dia: "funil: 3 alteradas, 31 no funil (1,4 s)".

    ⚠️ "No funil" é SEGUNDO A LISTAGEM do CV (CVDW), que atrasa - ver
    atualizar_listagem(). A situação real só vale depois da busca direta."""
    comeco = time.monotonic()
    token = _pegar_trava("funil")
    if token is None:
        return 0
    try:
        texto, codigo = rodar_funil_com_a_trava()
    finally:
        _soltar_trava(token)
    duracao = f"{time.monotonic() - comeco:.1f}".replace(".", ",")
    _linha_no_log(f"{texto} ({duracao} s)")
    return codigo


def rodar_funil_com_a_trava() -> tuple[str, int]:
    """O miolo do funil, para quem JÁ está com a trava: atualizar_funil() e o
    executor de reanálise ("Buscar reservas novas" do painel, 19/10/2026).
    Devolve (linha de resumo, código: 0 ok, 2 falhou)."""
    try:
        antes = _ler_listagem().get("reservas") or {}
        cfg = carregar_config()
        # A listagem completa (sem reservas_cv.json) avisa no terminal; aqui a
        # única saída é a linha do log.
        with open(os.devnull, "w") as nulo, contextlib.redirect_stdout(nulo):
            reservas, _ = atualizar_listagem(cfg)
        alteradas = sum(1 for idr, registro in reservas.items() if antes.get(idr) != registro)
        return f"funil: {alteradas} alteradas, {len(selecionar(reservas))} no funil", 0
    except (Exception, SystemExit) as e:
        return f"funil: FALHOU - {str(e).split(':', 1)[0][:150]}", 2


def _enviar_ao_servidor(motivo: str) -> None:
    """Opção B (só o site no servidor): manda planilhas e listagem pelo site.
    Modo local: não faz nada. Falha só vai para o log da sincronização."""
    try:
        import sincronizar_servidor
        sincronizar_servidor.sincronizar(motivo)
    except Exception:  # nunca derruba a varredura nem o funil
        pass


def _configuracao_do_servidor_ok() -> bool:
    """Modo explícito do envio (cliente_servidor.modo("envio"), que a varredura e
    o funil usam no fim): configuração parcial = NÃO rodam (falha fechada)."""
    import cliente_servidor
    try:
        cliente_servidor.modo("envio")
        return True
    except cliente_servidor.ConfiguracaoInvalida as exc:
        print(f"Configuração do servidor inválida - nada foi feito: {exc}", file=sys.stderr)
        try:
            PASTA_LOGS.mkdir(parents=True, exist_ok=True)
            with (PASTA_LOGS / f"varredura-{datetime.now():%Y-%m-%d}.txt").open("a", encoding="utf-8") as log:
                log.write(f"{datetime.now():%H:%M:%S} configuração do servidor inválida - nada foi feito: {exc}\n")
        except OSError:
            pass
        return False


def main() -> int:
    if not _configuracao_do_servidor_ok():
        return 2
    if "--atualizar-funil" in sys.argv[1:]:
        codigo = atualizar_funil()
        _enviar_ao_servidor("funil")
        return codigo
    PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    log = (PASTA_LOGS / f"varredura-{datetime.now():%Y-%m-%d}.txt").open("a", encoding="utf-8")
    log.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    sys.stdout = Saida(sys.__stdout__, log)
    sys.stderr = Saida(sys.__stderr__, log)
    try:
        codigo = rodar(so_listar="--so-listar" in sys.argv[1:], forcar="--forcar" in sys.argv[1:])
        _enviar_ao_servidor("varredura")
        return codigo
    finally:
        sys.stdout.flush()
        # Devolve os originais ANTES de fechar o log: senão o Python tenta
        # gravar no arquivo fechado ao sair.
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
        log.close()


if __name__ == "__main__":
    sys.exit(main())
