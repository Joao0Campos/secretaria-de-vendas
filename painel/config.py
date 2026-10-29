"""Configuração do painel, lida de variáveis de ambiente.

- SECRETARIA_SAIDAS: pasta saidas/ do projeto (padrão <projeto>/saidas).
- SECRETARIA_PAINEL_DB: banco SQLite das tratativas (padrão
  <projeto>/painel_dados/painel.db). painel_dados/ está no .gitignore.
- SECRETARIA_MINUTOS_MANUAIS_POR_RESERVA: opcional, inteiro - quanto tempo uma
  conferência manual leva. Sem ele vale MINUTOS_MANUAIS_PADRAO: o João estimou
  de 30 a 50 minutos por reserva (13/10/2026), e o painel usa o meio da faixa e
  mostra a faixa ao lado.
- SECRETARIA_COOKIE_SEGURO=1: etapa B (servidor atrás de proxy TLS). Liga o
  atributo Secure nos cookies e manda Strict-Transport-Security. Desligado por
  padrão porque em http://127.0.0.1 o navegador descartaria um cookie Secure.
- SECRETARIA_CV_URL_RESERVA: modelo do link
  "Abrir no CV", com {id} no lugar do número (padrão: CV_URL_RESERVA_PADRAO). Só vale
  https:// num subdomínio de cvcrm.com.br; qualquer outra coisa é ignorada, com
  aviso no terminal - o link sai do painel para fora, e não pode virar
  redirecionamento para outro site por um erro de configuração.

Servidor (20/10/2026). No PC do João nada muda: sem as variáveis abaixo o
painel escuta só em 127.0.0.1:8765 e ignora X-Forwarded-For, como sempre.
- SECRETARIA_PROXY_CONFIAVEL: IPs (ou redes) do proxy TLS na frente do painel,
  separados por vírgula (ex.: 127.0.0.1). Liga proxy_headers e
  forwarded_allow_ips no uvicorn: o IP real do cliente vem do X-Forwarded-For
  SÓ quando a conexão chega de um desses endereços, e é ele que o limite de 5
  tentativas de login usa. '*' é recusado (qualquer um forjaria o IP).
- SECRETARIA_HOST / SECRETARIA_PORTA: onde escutar (padrão 127.0.0.1:8765).
  Proxy na MESMA máquina (o nosso caso: Caddy no servidor) = host loopback,
  sempre: 0.0.0.0 abriria a porta 8765 na rede e daria para contornar o proxy
  e mandar senha por HTTP. Fora do loopback, só com o proxy em OUTRA máquina E
  SECRETARIA_PERMITIR_REDE=1, e o log avisa para liberar a porta no firewall
  só para o IP do proxy. Qualquer outra combinação: recusa e explica no log.
- SECRETARIA_EXECUTOR_CHAVE_HASH (opção B: só o site no servidor): sha256 em
  hex da chave do executor do PC. Liga as rotas /api/executor/* e /api/sync/*,
  que não usam sessão nem CSRF, só o cabeçalho "Authorization: Bearer <chave>".
  Sem ela, essas rotas não existem (404) e o executor usa o painel.db local.
  Gerar: python -m painel chave-executor.
- Backup diário do painel.db (painel/backup.py): ligado no servidor (com
  SECRETARIA_EXECUTOR_CHAVE_HASH) ou com SECRETARIA_BACKUP_DIARIO=1. No PC,
  sem as variáveis, nada muda.
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

RAIZ_PROJETO = Path(__file__).resolve().parent.parent

HOST = "127.0.0.1"
PORTA = 8765
HOSTS_LOCAIS = ("127.0.0.1", "localhost", "::1")

APP_NOME = "Secretaria de Vendas VIC"

# Conferência manual de uma reserva, estimada pelo João em 13/10/2026.
MINUTOS_MANUAIS_FAIXA = (30, 50)
MINUTOS_MANUAIS_PADRAO = 40

# Tela da reserva no CV, já na aba de documentos (é o que a conferência usa).
# Endereço copiado do navegador pelo João em 13/10/2026.
# Tela Impacto (/impacto, /api/impacto e o item de menu): OCULTA desde
# 20/10/2026 (decisão do João: ninguém usa a ferramenta ainda e os números não
# dizem nada). O código e os dados ficam; para religar, True aqui ou
# SECRETARIA_MOSTRAR_IMPACTO=1 no ambiente.
MOSTRAR_IMPACTO_PADRAO = False

CV_URL_RESERVA_PADRAO: str | None = (
    "https://suaempresa.cvcrm.com.br/gestor/comercial/reservas/{id}/administrar#index_documentos")
RE_HOST_CV = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+cvcrm\.com\.br")


def proxies_confiaveis() -> list[str]:
    """SECRETARIA_PROXY_CONFIAVEL em lista ([] = sem proxy, como no PC do João).
    Endereço inválido ou '*': SystemExit com a explicação."""
    bruto = os.environ.get("SECRETARIA_PROXY_CONFIAVEL") or ""
    itens = [i.strip() for i in re.split(r"[,\s]+", bruto) if i.strip()]
    for item in itens:
        if item == "*":
            raise SystemExit("SECRETARIA_PROXY_CONFIAVEL='*' recusado: qualquer um poderia forjar o IP do "
                             "cliente no X-Forwarded-For. Liste o IP do proxy (ex.: 127.0.0.1).")
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise SystemExit(f"SECRETARIA_PROXY_CONFIAVEL: {item!r} não é um IP nem uma rede (ex.: 127.0.0.1, "
                             "10.0.0.0/24).") from None
    return itens


def _e_loopback(item: str) -> bool:
    return ipaddress.ip_network(item, strict=False).is_loopback


def endereco_de_escuta() -> tuple[str, int]:
    """(host, porta) de SECRETARIA_HOST / SECRETARIA_PORTA, padrão 127.0.0.1:8765.
    Recusa (SystemExit com a explicação) porta inválida e host fora do
    loopback, a não ser com o proxy em outra máquina e SECRETARIA_PERMITIR_REDE=1
    (ver o topo deste arquivo)."""
    host = (os.environ.get("SECRETARIA_HOST") or HOST).strip()
    porta_bruta = (os.environ.get("SECRETARIA_PORTA") or str(PORTA)).strip()
    if not porta_bruta.isdigit() or not 1 <= int(porta_bruta) <= 65535:
        raise SystemExit(f"SECRETARIA_PORTA={porta_bruta!r} recusada: precisa ser um número de 1 a 65535.")
    if host.lower() in HOSTS_LOCAIS:
        return host, int(porta_bruta)
    proxies = proxies_confiaveis()
    if not proxies:
        raise SystemExit(
            f"SECRETARIA_HOST={host!r} recusado: fora de 127.0.0.1 o painel só sobe atrás de um proxy TLS, "
            "e para isso SECRETARIA_PROXY_CONFIAVEL precisa dizer o IP do proxy. "
            "Sem proxy, deixe SECRETARIA_HOST vazio (127.0.0.1).")
    if all(_e_loopback(p) for p in proxies):
        raise SystemExit(
            f"SECRETARIA_HOST={host!r} recusado: o proxy confiável é desta máquina "
            f"({', '.join(proxies)}), então o painel escuta só em 127.0.0.1 - fora dele a porta {porta_bruta} "
            "ficaria aberta na rede e daria para contornar o proxy (e mandar senha por HTTP). "
            "Deixe SECRETARIA_HOST vazio.")
    if (os.environ.get("SECRETARIA_PERMITIR_REDE") or "").strip() != "1":
        raise SystemExit(
            f"SECRETARIA_HOST={host!r} recusado: com o proxy em outra máquina, escutar na rede exige "
            "SECRETARIA_PERMITIR_REDE=1 e a porta liberada no firewall só para o IP do proxy.")
    return host, int(porta_bruta)


def aviso_de_rede(host: str, porta: int) -> str | None:
    """Texto para o log quando o painel escuta fora do loopback (proxy em
    outra máquina, SECRETARIA_PERMITIR_REDE=1)."""
    if host.lower() in HOSTS_LOCAIS:
        return None
    return (f"ATENÇÃO: o painel escuta em {host}:{porta}, fora do loopback. Libere a porta {porta} no "
            f"firewall SÓ para o IP do proxy ({', '.join(proxies_confiaveis())}); qualquer outra máquina "
            "chegaria ao painel sem TLS.")


@dataclass(frozen=True)
class Config:
    saidas: Path
    db: Path
    minutos_manuais_por_reserva: int | None = None
    cookie_seguro: bool = False
    cv_url_reserva: str | None = None
    mostrar_impacto: bool = MOSTRAR_IMPACTO_PADRAO
    executor_chave_hash: str | None = None
    backup_diario: bool = False


def _inteiro_opcional(valor: str | None) -> int | None:
    if valor is None or not valor.strip():
        return None
    try:
        n = int(valor.strip())
    except ValueError:
        raise SystemExit("SECRETARIA_MINUTOS_MANUAIS_POR_RESERVA precisa ser um número inteiro de minutos")
    return n if n > 0 else None


def modelo_cv(valor: str | None) -> str | None:
    """O modelo, se for https:// num subdomínio de cvcrm.com.br com {id} (e
    nenhuma outra chave) - senão None."""
    valor = (valor or "").strip()
    if not valor or "{id}" not in valor:
        return None
    if re.search(r"[{}]", valor.replace("{id}", "")) or re.search(r"[\s\\]", valor):
        return None
    try:
        partes = urlsplit(valor.format(id=1))
        host, porta = partes.hostname, partes.port
    except ValueError:
        return None
    if (partes.scheme != "https" or partes.username is not None or partes.password is not None
            or porta not in (None, 443) or not host or not RE_HOST_CV.fullmatch(host)
            or partes.netloc.lower() not in (host, f"{host}:443")):
        return None
    return valor


def _modelo_cv_do_ambiente(variavel: str, padrao: str | None) -> str | None:
    bruto = os.environ.get(variavel)
    if bruto is None or not bruto.strip():
        bruto = padrao
    if not bruto:
        return None
    modelo = modelo_cv(bruto)
    if modelo is None:
        print(f"Aviso: {variavel} ignorada - precisa ser https://<algo>.cvcrm.com.br/... com {{id}} "
              "no lugar do número. O link \"Abrir no CV\" fica desligado.", file=sys.stderr)
    return modelo


def _hash_da_chave_do_ambiente() -> str | None:
    bruto = (os.environ.get("SECRETARIA_EXECUTOR_CHAVE_HASH") or "").strip().lower()
    if not bruto:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", bruto):
        print("Aviso: SECRETARIA_EXECUTOR_CHAVE_HASH ignorada - precisa ser o sha256 em hex (64 caracteres) "
              "da chave. As rotas do executor ficam desligadas.", file=sys.stderr)
        return None
    return bruto


def carregar() -> Config:
    chave_hash = _hash_da_chave_do_ambiente()
    saidas = os.environ.get("SECRETARIA_SAIDAS") or str(RAIZ_PROJETO / "saidas")
    db = os.environ.get("SECRETARIA_PAINEL_DB") or str(RAIZ_PROJETO / "painel_dados" / "painel.db")
    return Config(
        saidas=Path(saidas),
        db=Path(db),
        minutos_manuais_por_reserva=_inteiro_opcional(
            os.environ.get("SECRETARIA_MINUTOS_MANUAIS_POR_RESERVA")) or MINUTOS_MANUAIS_PADRAO,
        cookie_seguro=(os.environ.get("SECRETARIA_COOKIE_SEGURO") or "").strip() == "1",
        cv_url_reserva=_modelo_cv_do_ambiente("SECRETARIA_CV_URL_RESERVA", CV_URL_RESERVA_PADRAO),
        mostrar_impacto=(os.environ.get("SECRETARIA_MOSTRAR_IMPACTO") or "").strip() == "1"
        or MOSTRAR_IMPACTO_PADRAO,
        executor_chave_hash=chave_hash,
        backup_diario=bool(chave_hash) or (os.environ.get("SECRETARIA_BACKUP_DIARIO") or "").strip() == "1",
    )
