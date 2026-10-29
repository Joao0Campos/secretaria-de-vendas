"""Congelar / descongelar os escritores do PC na virada (opção B, 20/10/2026).

Na virada, a cópia final do painel.db do PC para o servidor só pode acontecer
com TODOS os escritores do PC parados: o site do PC (tratativas), o executor
(fila), o funil e a varredura (trava, listagem). Senão, alguém grava no banco
velho depois da cópia, e haveria dois bancos.

    python -m painel congelar-pc
        1. desabilita as tarefas Painel, Executor, Funil e Varredura diaria
           (nada novo sobe);
        2. confere que não há trava (varredura.trava) nem pedido em_andamento:
           se houver, NÃO mata ninguém - avisa para esperar e rodar de novo
           (as tarefas continuam desabilitadas);
        3. encerra o site e o executor do PC (pythonw ...painel_servidor.pyw /
           executor_reanalise) e confere que nenhum dos quatro ficou rodando.
        Só termina com "PC congelado" quando está tudo conferido.

    python -m painel descongelar-pc [--com-painel]
        habilita Executor, Funil e Varredura diaria (e o Painel só com
        --com-painel, para desfazer a virada) e mostra o estado.

Só desabilita/habilita tarefas e encerra os dois processos residentes do
projeto: não apaga nada nem mexe no banco.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

PASTA_DAS_TAREFAS = "\\Secretaria de Vendas\\"
TAREFAS = ("Painel", "Executor", "Funil", "Varredura diaria")
TAREFAS_QUE_VOLTAM = ("Executor", "Funil", "Varredura diaria")  # o Painel fica desligado depois da virada
PROCESSOS = ("painel_servidor.pyw", "executor_reanalise", "varredura_diaria.py", "-m painel servir")


def _rodar(comando: list[str]) -> tuple[int, str]:
    r = subprocess.run(comando, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class ConsultaFalhou(RuntimeError):
    """Não deu para confirmar quais processos estão rodando."""


def _processos_do_projeto() -> list[dict]:
    """[{pid, linha}] dos processos Python do projeto que escrevem (site,
    executor, varredura/funil). FALHA FECHADA (2ª confirmação da Turing): só
    aceita código 0 e um JSON que seja uma lista - '[]' quando não há nenhum,
    graças ao -InputObject @(...). Erro, saída vazia ou formato inesperado =
    ConsultaFalhou, e ninguém é declarado congelado."""
    script = ("ConvertTo-Json -Compress -InputObject @(Get-CimInstance Win32_Process "
              "-Filter \"Name like 'python%'\" | Select-Object ProcessId, CommandLine)")
    codigo, saida = _rodar(["powershell", "-NoProfile", "-Command", script])
    if codigo != 0:
        raise ConsultaFalhou(f"PowerShell saiu com código {codigo}: {saida.strip()[:200]}")
    try:
        dados = json.loads(saida)
        if not isinstance(dados, list):
            raise ValueError("não é uma lista")
        processos = [{"pid": int(d["ProcessId"]), "linha": d.get("CommandLine") or ""} for d in dados]
    except (ValueError, TypeError, KeyError) as exc:
        raise ConsultaFalhou(f"resposta inesperada do PowerShell ({exc}): {saida.strip()[:200]!r}") from None
    return [p for p in processos if any(nome in p["linha"] for nome in PROCESSOS)]


def _encerrar(pid: int) -> None:
    _rodar(["taskkill", "/PID", str(pid), "/T", "/F"])


def _tarefa(nome: str, habilitar: bool) -> tuple[bool, str]:
    codigo, saida = _rodar(["schtasks", "/Change", "/TN", PASTA_DAS_TAREFAS + nome,
                            "/ENABLE" if habilitar else "/DISABLE"])
    return codigo == 0, saida.strip()


def _pedidos_em_andamento(db: Path) -> int:
    if not Path(db).exists():
        return 0
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        return con.execute("SELECT COUNT(*) FROM pedidos_reanalise WHERE status = 'em_andamento'").fetchone()[0]
    except sqlite3.OperationalError:  # banco sem a fila
        return 0
    finally:
        con.close()


def congelar(db: Path, saida=print) -> bool:
    """True = PC congelado e conferido; pode copiar o painel.db."""
    falhas = []
    for nome in TAREFAS:
        ok, msg = _tarefa(nome, habilitar=False)
        saida(f"tarefa {nome}: {'desabilitada' if ok else 'NÃO desabilitada - ' + msg}")
        if not ok:
            falhas.append(nome)
    if falhas:
        saida("PARE: nem todas as tarefas foram desabilitadas (rode como o usuário dono delas).")
        return False
    trava = Path(db).parent / "varredura.trava"
    if trava.exists():
        saida(f"ESPERE: a trava está com {trava.read_text(encoding='utf-8', errors='replace')[:200]!r} "
              "(varredura, funil ou reanálise rodando). Rode congelar-pc de novo quando ela sumir.")
        return False
    em_andamento = _pedidos_em_andamento(db)
    if em_andamento:
        saida(f"ESPERE: {em_andamento} pedido(s) em andamento no painel.db. Rode de novo quando terminarem.")
        return False
    try:
        for processo in _processos_do_projeto():
            saida(f"encerrando PID {processo['pid']}: {processo['linha'][:120]}")
            _encerrar(processo["pid"])
        restantes = _processos_do_projeto()
    except ConsultaFalhou as exc:
        saida(f"PARE: não consegui confirmar os processos do PC - NÃO está congelado. ({exc})")
        return False
    if restantes:
        saida(f"PARE: ainda rodando: {', '.join(str(p['pid']) for p in restantes)}")
        return False
    if trava.exists() or _pedidos_em_andamento(db):
        saida("PARE: apareceu trava ou pedido em andamento durante o congelamento. Rode de novo.")
        return False
    saida("PC congelado: tarefas desabilitadas, nenhum processo do projeto, sem trava e sem pedido em "
          "andamento. Pode fazer a cópia final do painel.db.")
    return True


def descongelar(com_painel: bool = False, saida=print) -> bool:
    ok_geral = True
    for nome in TAREFAS_QUE_VOLTAM + (("Painel",) if com_painel else ()):
        ok, msg = _tarefa(nome, habilitar=True)
        saida(f"tarefa {nome}: {'habilitada' if ok else 'NÃO habilitada - ' + msg}")
        ok_geral &= ok
    if not com_painel:
        saida("tarefa Painel: continua desabilitada (o site agora é o do servidor).")
    saida("Saia e entre no Windows (ou rode a tarefa Executor) para o executor subir com as variáveis novas.")
    return ok_geral
