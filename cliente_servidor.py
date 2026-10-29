"""
Cliente do site no servidor (opção B, decisão do João em 20/10/2026)
====================================================================

Só o SITE fica no servidor (SERVIDOR_INTERNO); a varredura, o funil e o executor
continuam no PC do João. Este módulo é o único caminho do PC até o site: HTTPS,
com a chave do executor no cabeçalho Authorization (o servidor guarda só o hash).
A porta 445 (compartilhamento) não passa pela VPN, por isso não há robocopy:
os arquivos também vão pelo site (sincronizar_servidor.py).

Modo POR COMPONENTE (revisão de arquitetura da Turing + ensaio, 20/10/2026):
- SECRETARIA_MODO_ENVIO=remoto|local: o envio ao servidor (sincronizar_servidor.py,
  chamado também pela varredura e pelo funil);
- SECRETARIA_MODO_EXECUTOR=remoto|local: a fila do executor (painel.db do PC ou a
  API do site). No ensaio só o envio é remoto; o executor vira remoto na virada,
  e remoto exige o envio remoto (ele só conclui depois de enviar a planilha).
Sem a variável do componente e sem NENHUMA variável remota: local, como sempre.
Configuração parcial ou contraditória - variável remota definida sem o modo do
componente, remoto sem a URL ou a chave, valor desconhecido - é FALHA FECHADA:
modo() levanta ConfiguracaoInvalida e quem chamou para com o motivo, sem cair no
painel.db local. Local EXPLÍCITO com as variáveis remotas definidas é válido (é
o executor durante o ensaio).

Variáveis remotas (no PC), comuns aos dois componentes:
- SECRETARIA_SERVIDOR_URL: ex. https://SERVIDOR_INTERNO (ou o nome que a TI der).
  SÓ https: a chave vai no cabeçalho, e http a mandaria em claro pela rede.
  Não há exceção (nem por variável de ambiente); os testes usam https://testserver.
- SECRETARIA_EXECUTOR_CHAVE: a chave (python -m painel chave-executor).
- SECRETARIA_CA_BUNDLE: o certificado raiz da CA interna do Caddy (.crt/.pem),
  para conferir o certificado do servidor. Sem ela, vale o repositório padrão.

A chave nunca vai para log nem para mensagem de erro.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import requests

TIMEOUT = (10, 120)  # (conexão, leitura) em s: um documento de 16 MB pela VPN cabe com folga


VARIAVEIS_REMOTAS = ("SECRETARIA_SERVIDOR_URL", "SECRETARIA_EXECUTOR_CHAVE", "SECRETARIA_CA_BUNDLE")
OBRIGATORIAS_NO_REMOTO = ("SECRETARIA_SERVIDOR_URL", "SECRETARIA_EXECUTOR_CHAVE")


class ServidorIndisponivel(RuntimeError):
    """Rede, certificado, chave recusada ou resposta de erro do site."""


class ConfiguracaoInvalida(RuntimeError):
    """Configuração parcial ou contraditória: ninguém roda (falha fechada)."""


def _url_https(url: str) -> bool:
    partes = urlsplit(url.strip())
    return partes.scheme.lower() == "https" and bool(partes.hostname)


COMPONENTES = {"envio": "SECRETARIA_MODO_ENVIO", "executor": "SECRETARIA_MODO_EXECUTOR"}


def modo(componente: str) -> str:
    """'local' ou 'remoto' para 'envio' ou 'executor'. ConfiguracaoInvalida em
    qualquer configuração parcial (ver o topo do arquivo). A mensagem nunca traz
    o valor da chave."""
    variavel = COMPONENTES[componente]
    pedido = (os.environ.get(variavel) or "").strip().lower()
    definidas = [v for v in VARIAVEIS_REMOTAS if (os.environ.get(v) or "").strip()]
    if pedido not in ("", "local", "remoto"):
        raise ConfiguracaoInvalida(f"{variavel}={pedido!r} não existe: use local ou remoto")
    if pedido == "remoto":
        faltam = [v for v in OBRIGATORIAS_NO_REMOTO if v not in definidas]
        if faltam:
            raise ConfiguracaoInvalida(f"{variavel}=remoto sem {', '.join(faltam)}")
        if not _url_https(os.environ["SECRETARIA_SERVIDOR_URL"]):
            raise ConfiguracaoInvalida("SECRETARIA_SERVIDOR_URL precisa ser https:// (a chave não pode ir em "
                                       "claro pela rede)")
        if componente == "executor" and modo("envio") != "remoto":
            raise ConfiguracaoInvalida("SECRETARIA_MODO_EXECUTOR=remoto exige SECRETARIA_MODO_ENVIO=remoto: "
                                       "o executor só conclui um pedido depois de enviar a planilha")
        return "remoto"
    if pedido == "" and definidas:
        raise ConfiguracaoInvalida(
            f"{', '.join(definidas)} definida(s) sem {variavel}: configuração parcial. Defina {variavel}=remoto "
            f"ou {variavel}=local, ou apague as variáveis remotas")
    return "local"


class Cliente:
    def __init__(self, url: str | None = None, chave: str | None = None, ca: str | None = None,
                 sessao: requests.Session | None = None):
        self.url = (url or os.environ.get("SECRETARIA_SERVIDOR_URL") or "").strip().rstrip("/")
        chave = (chave or os.environ.get("SECRETARIA_EXECUTOR_CHAVE") or "").strip()
        if not self.url or not chave:
            raise ServidorIndisponivel("SECRETARIA_SERVIDOR_URL e SECRETARIA_EXECUTOR_CHAVE são obrigatórias")
        if not _url_https(self.url):
            raise ServidorIndisponivel("SECRETARIA_SERVIDOR_URL precisa ser https:// (a chave não pode ir em "
                                       "claro pela rede)")
        self.verify: str | bool = (ca or os.environ.get("SECRETARIA_CA_BUNDLE") or "").strip() or True
        self.sessao = sessao or requests.Session()
        self._cabecalhos = {"Authorization": f"Bearer {chave}"}

    def _pedir(self, metodo: str, rota: str, **k) -> dict:
        try:
            resp = self.sessao.request(metodo, self.url + rota, headers={**self._cabecalhos, **k.pop("headers", {})},
                                       timeout=TIMEOUT, verify=self.verify, **k)
        except requests.RequestException as exc:
            raise ServidorIndisponivel(f"{rota}: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            detalhe = ""
            try:
                detalhe = str(resp.json().get("detail") or "")[:200]
            except ValueError:
                pass
            raise ServidorIndisponivel(f"{rota}: HTTP {resp.status_code} {detalhe}".strip())
        try:
            return resp.json()
        except ValueError:
            raise ServidorIndisponivel(f"{rota}: resposta sem JSON") from None

    def baixar(self, rota: str, destino: Path) -> dict:
        """GET em streaming para 'destino'. Devolve os cabeçalhos da resposta."""
        try:
            resp = self.sessao.request("GET", self.url + rota, headers=self._cabecalhos, timeout=TIMEOUT,
                                       verify=self.verify, stream=True)
        except requests.RequestException as exc:
            raise ServidorIndisponivel(f"{rota}: {type(exc).__name__}") from None
        try:
            if resp.status_code >= 400:
                raise ServidorIndisponivel(f"{rota}: HTTP {resp.status_code}")
            try:
                with open(destino, "wb") as f:
                    for pedaco in resp.iter_content(1 << 20):
                        f.write(pedaco)
            except requests.RequestException as exc:
                raise ServidorIndisponivel(f"{rota}: {type(exc).__name__}") from None
            return dict(resp.headers)
        finally:
            resp.close()

    def post(self, rota: str, dados: dict | None = None) -> dict:
        return self._pedir("POST", rota, json=dados or {})

    def enviar_arquivo(self, caminho: str, local: Path, tamanho: int, mtime_ns: int, sha256: str) -> dict:
        with open(local, "rb") as f:
            return self._pedir("PUT", "/api/sync/arquivo", data=f,
                               params={"caminho": caminho, "tamanho": tamanho, "mtime_ns": mtime_ns,
                                       "sha256": sha256},
                               headers={"Content-Length": str(tamanho), "Content-Type": "application/octet-stream"})
