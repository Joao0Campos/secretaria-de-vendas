"""Sobe o painel sem janela (pythonw), para o Agendador de Tarefas abrir junto
com o Windows (tarefa "Secretaria de Vendas\\Painel", gatilho: ao fazer logon).
No PC do João escuta só em 127.0.0.1:8765; no servidor, onde
SECRETARIA_HOST/SECRETARIA_PORTA mandarem (config.endereco_de_escuta()).

Sem console, o que o servidor imprimiria vai para painel_dados/logs/painel.txt.
Se a porta já estiver em uso (o painel já está aberto), sai em silêncio.
"""
import socket
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from painel import config  # noqa: E402


def abrir_log():
    logs = RAIZ / "painel_dados" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return open(logs / "painel.txt", "a", encoding="utf-8", buffering=1)


try:
    host, porta = config.endereco_de_escuta()
except SystemExit as exc:  # configuração recusada: explica no log, que é o único lugar visível
    abrir_log().write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S} painel NÃO subiu: {exc}\n")
    sys.exit(2)

with socket.socket() as s:
    if s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "::") else host, porta)) == 0:
        sys.exit(0)

saida = abrir_log()
saida.write(f"\n===== painel iniciado em {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
sys.stdout = sys.stderr = saida

from painel.__main__ import servir  # noqa: E402

servir()
