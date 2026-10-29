"""Sobe o executor de reanálise sem janela (pythonw), para o Agendador de Tarefas
abrir junto com o Windows (tarefa "Secretaria de Vendas\\Executor", gatilho: ao
fazer logon), como o painel_servidor.pyw faz com o painel.

O log de cada pedido vai para painel_dados/logs/reanalise-AAAA-MM-DD.txt (o
próprio executor grava). Sem console, o resto - a linha de início, um erro
inesperado ao subir - vai para painel_dados/logs/executor.txt.
Se já houver outro executor rodando (mutex com nome, ver
executor_reanalise.instancia_unica()), sai em silêncio.
"""
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

logs = RAIZ / "painel_dados" / "logs"
logs.mkdir(parents=True, exist_ok=True)
saida = open(logs / "executor.txt", "a", encoding="utf-8", buffering=1)
saida.write(f"\n===== executor iniciado em {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
sys.stdout = sys.stderr = saida

from executor_reanalise import main  # noqa: E402

sys.exit(main([], eco=False))
