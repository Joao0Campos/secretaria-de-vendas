"""Blindagem carregada ANTES de qualquer import do projeto.

- bloqueia rede (socket, requests) e o cliente Gemini (google.genai);
- bloqueia leitura de config.json, saidas/, backups_pessoa/, investigacao_renda/
  e de PDF/DOCX/MP4; bloqueia QUALQUER escrita dentro da pasta do projeto;
- desliga geração de bytecode (além do -B na linha de comando);
- impede importar testar_alteracao_pessoa.
Toda tentativa bloqueada é registrada em TENTATIVAS_BLOQUEADAS e levanta erro.
"""
from __future__ import annotations

import builtins
import importlib.abc
import io
import os
import socket
import sys
import types
from pathlib import Path

sys.dont_write_bytecode = True

# Raiz do projeto pela posição deste arquivo (painel/tests/), e não pelo caminho do
# Desktop do João: 19/10/2026, os testes vieram do espelho do Maestri para o projeto
# e precisam rodar igual na máquina da TI.
PROJETO = Path(__file__).resolve().parents[2]
PROIBIDOS_DIR = {"saidas", "backups_pessoa", "investigacao_renda"}
PROIBIDOS_EXT = {".pdf", ".docx", ".mp4"}
TENTATIVAS_BLOQUEADAS: list[str] = []


class Bloqueado(RuntimeError):
    pass


def _bloquear(motivo: str):
    TENTATIVAS_BLOQUEADAS.append(motivo)
    raise Bloqueado(motivo)


# ---------------------------------------------------------------- rede
def _sem_rede(*a, **k):
    _bloquear(f"rede: {a[:2]!r}")


socket.socket.connect = _sem_rede  # type: ignore[assignment]
socket.socket.connect_ex = _sem_rede  # type: ignore[assignment]
socket.create_connection = _sem_rede  # type: ignore[assignment]
socket.getaddrinfo = _sem_rede  # type: ignore[assignment]

import requests  # noqa: E402  (do .venv do projeto, só para poder bloquear)
import requests.api  # noqa: E402
import requests.sessions  # noqa: E402

requests.sessions.Session.request = lambda self, method, url, *a, **k: _bloquear(f"requests {method} {url}")  # type: ignore
requests.api.request = lambda method, url, **k: _bloquear(f"requests {method} {url}")  # type: ignore
for _verbo in ("get", "post", "put", "patch", "delete", "head", "options"):
    setattr(requests, _verbo, lambda url, *a, _v=_verbo, **k: _bloquear(f"requests {_v} {url}"))


# ---------------------------------------------------------------- Gemini
class _GenaiBloqueado(types.ModuleType):
    def __getattr__(self, nome):
        _bloquear(f"google.genai.{nome}")


_genai = _GenaiBloqueado("google.genai")
_genai.__path__ = []  # parece pacote: 'from google.genai import types' cai aqui também
sys.modules["google.genai"] = _genai
sys.modules["google.genai.types"] = _GenaiBloqueado("google.genai.types")
try:
    import google  # namespace real do .venv

    google.genai = _genai  # type: ignore[attr-defined]
except ImportError:
    _g = types.ModuleType("google")
    _g.__path__ = []
    _g.genai = _genai  # type: ignore[attr-defined]
    sys.modules["google"] = _g


# ---------------------------------------------------------------- arquivos
def _checar_caminho(arquivo, modo: str = "r"):
    if isinstance(arquivo, int):
        return
    try:
        p = Path(os.fsdecode(arquivo)).resolve()
    except Exception:
        return
    dentro = p == PROJETO or PROJETO in p.parents
    if not dentro:
        return
    rel = p.relative_to(PROJETO)
    if any(c in modo for c in "wax+"):
        _bloquear(f"escrita no projeto: {rel}")
    if p.name.lower() == "config.json":
        _bloquear("leitura de config.json")
    if rel.parts and rel.parts[0].lower() in PROIBIDOS_DIR:
        _bloquear(f"leitura de dado de cliente: {rel.parts[0]}/...")
    if p.suffix.lower() in PROIBIDOS_EXT:
        _bloquear(f"leitura de arquivo {p.suffix}")


_open_original = builtins.open


def _open_vigiado(arquivo, modo="r", *a, **k):
    _checar_caminho(arquivo, modo)
    return _open_original(arquivo, modo, *a, **k)


builtins.open = _open_vigiado
io.open = _open_vigiado

_os_open_original = os.open


def _os_open_vigiado(caminho, flags, *a, **k):
    escrita = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)
    _checar_caminho(caminho, "w" if escrita else "r")
    return _os_open_original(caminho, flags, *a, **k)


os.open = _os_open_vigiado

_mkdir_original = Path.mkdir


def _mkdir_vigiado(self, *a, **k):
    _checar_caminho(self, "w")
    return _mkdir_original(self, *a, **k)


Path.mkdir = _mkdir_vigiado  # type: ignore[assignment]


# ---------------------------------------------------------------- import proibido
class _ImpedeEscritor(importlib.abc.MetaPathFinder):
    def find_spec(self, nome, caminho=None, alvo=None):
        if nome.split(".")[0] in ("testar_alteracao_pessoa", "API_test"):
            _bloquear(f"import proibido: {nome}")
        return None


sys.meta_path.insert(0, _ImpedeEscritor())

# projeto no sys.path só depois de tudo bloqueado - no FIM, não no começo:
# rodando da pasta de trabalho, quem tem que ser importado é a cópia editada
# que está ao lado deste arquivo, não o original (13/10/2026: com insert(0) os
# testes da pasta de trabalho carregavam o analisar_documentos.py do Desktop).
if str(PROJETO) not in sys.path:
    sys.path.append(str(PROJETO))


def neutralizar_config():
    """Depois do import: qualquer função que tente carregar o config explode."""
    import mvp_v1_validador as v1

    def _sem_config():
        _bloquear("carregar_config()")

    v1.carregar_config = _sem_config
    for nome in ("mvp_v2_reserva", "analisar_documentos", "gerar_consolidado"):
        m = sys.modules.get(nome)
        if m is not None and hasattr(m, "carregar_config"):
            m.carregar_config = _sem_config
