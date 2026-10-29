"""Painel da Secretaria de Vendas VIC.

Site LOCAL (127.0.0.1) que mostra, por reserva, os apontamentos que as
planilhas de saidas/ já trazem, guarda a TRATATIVA de cada um num SQLite e mede
o impacto da ferramenta.

Somente leitura sobre saidas/: não chama o CV nem a IA e não altera planilha.
A única coisa que o painel escreve é o próprio banco (painel_dados/).
"""
import sys
from pathlib import Path

# O painel usa os módulos do projeto (analisar_documentos, testar_amostra), que
# moram na pasta acima deste pacote - inclusive se for chamado de outra pasta.
_RAIZ = str(Path(__file__).resolve().parent.parent)
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)
