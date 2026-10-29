"""Recebimento dos arquivos que o PC do João envia ao servidor (opção B, 20/10/2026).

Só o SITE fica no servidor; a varredura, o funil e o executor rodam no PC. O PC
manda, em mão única e pelo próprio site (HTTPS, a porta 445 não passa na VPN):
saidas/reserva/<pasta>/<arquivo> e três arquivos de painel_dados/. Ele manda o
manifesto (caminho, tamanho, mtime, sha256), o site responde o que falta ou
mudou, e o PC envia só isso, um arquivo por pedido.

Segurança (o caminho vem de fora):
- caminho relativo com '/', sem '..', '.', vazio, absoluto, unidade, '\\', ':',
  caractere de controle ou nome reservado do Windows; só
  'saidas/reserva/<pasta>/<arquivo>' ou 'painel_dados/<um dos ARQUIVOS_DE_DADOS>';
  e o caminho resolvido tem de ficar sob a raiz (defesa extra);
- arquivo até LIMITE_ARQUIVO, gravado num temporário ao lado, conferido
  (tamanho e sha256) e só então trocado com os.replace: quem lê nunca vê
  arquivo pela metade; o mtime do PC é preservado (o painel usa o mtime);
- apagar no servidor: só arquivo de saidas/reserva que sumiu do PC, só com o
  manifesto marcado como completo (o PC leu tudo sem falha) e nunca com um
  manifesto sem nenhum arquivo de saidas/reserva;
- corpo lido em pedaços com teto rígido (arquivo e manifesto), com ou sem
  Content-Length: passou, para na hora (413).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from pathlib import Path

LIMITE_ARQUIVO = 64 * 1024 * 1024        # o maior documento em 20/10/2026 tinha 16 MB
LIMITE_MANIFESTO = 8 * 1024 * 1024       # ~2.100 arquivos = ~0,5 MB
MAXIMO_ITENS_MANIFESTO = 50_000
ARQUIVOS_DE_DADOS = ("reservas_cv.json", "varreduras.jsonl", "ultima_varredura_ok.txt", "ia_estado.json")
# Arquivos de painel_dados/ com nome por padrão: o consumo da IA, um por mês.
RE_DADOS_POR_PADRAO = re.compile(r"consumo_ia-\d{4}-\d{2}\.jsonl")


def dado_permitido(nome: str) -> bool:
    return nome in ARQUIVOS_DE_DADOS or bool(RE_DADOS_POR_PADRAO.fullmatch(nome))
SUFIXO_PARCIAL = ".parcial"
_PROIBIDO = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_RESERVADOS = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_SHA256 = re.compile(r"[0-9a-f]{64}")


class CaminhoInvalido(ValueError):
    pass


class ArquivoRecusado(ValueError):
    pass


class CorpoGrandeDemais(ArquivoRecusado):
    """Passou do teto (ou do tamanho declarado): o envio para na hora (413)."""


async def ler_com_teto(fluxo, teto: int) -> bytes:
    """Lê o corpo em pedaços e PARA ao passar do teto - não depende do
    Content-Length (sem ele, request.body() carregaria tudo antes de conferir)."""
    partes, total = [], 0
    async for pedaco in fluxo:
        total += len(pedaco)
        if total > teto:
            raise CorpoGrandeDemais(f"corpo acima de {teto} bytes")
        partes.append(pedaco)
    return b"".join(partes)


def sha256_do_arquivo(caminho: Path) -> str:
    resumo = hashlib.sha256()
    with open(caminho, "rb") as f:
        for pedaco in iter(lambda: f.read(1 << 20), b""):
            resumo.update(pedaco)
    return resumo.hexdigest()


def _parte_valida(parte: str) -> bool:
    return (parte not in ("", ".", "..") and not _PROIBIDO.search(parte) and parte.rstrip(" .") == parte
            and parte.split(".")[0].lower() not in _RESERVADOS and not parte.endswith(SUFIXO_PARCIAL))


def destino(caminho, saidas: Path, dados: Path) -> Path:
    """Onde gravar 'caminho' (relativo, como o PC manda). CaminhoInvalido se não pode."""
    if not isinstance(caminho, str) or not 0 < len(caminho) <= 400 or caminho.startswith("/"):
        raise CaminhoInvalido(caminho)
    partes = caminho.split("/")
    if not all(_parte_valida(p) for p in partes):
        raise CaminhoInvalido(caminho)
    if len(partes) == 2 and partes[0] == "painel_dados" and dado_permitido(partes[1]):
        raiz, alvo = Path(dados), Path(dados) / partes[1]
    elif len(partes) == 4 and partes[:2] == ["saidas", "reserva"]:
        raiz = Path(saidas) / "reserva"
        alvo = raiz / partes[2] / partes[3]
    else:
        raise CaminhoInvalido(caminho)
    if raiz.resolve() not in alvo.resolve().parents:
        raise CaminhoInvalido(caminho)
    return alvo


def _arquivos_de_saidas(saidas: Path) -> dict[str, Path]:
    raiz = Path(saidas) / "reserva"
    if not raiz.is_dir():
        return {}
    return {f"saidas/reserva/{p.parent.name}/{p.name}": p
            for p in raiz.glob("*/*") if p.is_file() and not p.name.endswith(SUFIXO_PARCIAL)}


def comparar(manifesto: list, saidas: Path, dados: Path) -> dict:
    """{'faltam': caminhos a enviar (não existem, tamanho/mtime diferentes ou,
    quando o item traz sha256 - a conferência completa -, conteúdo diferente),
    'sobrando': arquivos de saidas/reserva que sumiram do PC, 'invalidos'}."""
    if not isinstance(manifesto, list):
        raise ArquivoRecusado("manifesto precisa ser uma lista")
    if len(manifesto) > MAXIMO_ITENS_MANIFESTO:
        raise CorpoGrandeDemais(f"manifesto acima de {MAXIMO_ITENS_MANIFESTO} itens")
    faltam, ausentes, invalidos, vistos = [], [], [], set()
    for item in manifesto:
        try:
            caminho = item["caminho"]
            alvo = destino(caminho, saidas, dados)
            tamanho, mtime_ns = int(item["tamanho"]), int(item["mtime_ns"])
        except (CaminhoInvalido, KeyError, TypeError, ValueError):
            invalidos.append(str(item.get("caminho") if isinstance(item, dict) else item)[:200])
            continue
        vistos.add(caminho)
        try:
            st = alvo.stat()
            igual = st.st_size == tamanho and st.st_mtime_ns == mtime_ns
            sha = item.get("sha256")
            if igual and isinstance(sha, str) and sha:
                igual = sha256_do_arquivo(alvo) == sha.lower()
        except FileNotFoundError:
            igual = False
            ausentes.append(caminho)
        except OSError:
            igual = False
        if not igual:
            faltam.append(caminho)
    no_servidor = _arquivos_de_saidas(saidas)
    tem_saidas = any(c.startswith("saidas/") for c in vistos)
    sobrando = sorted(c for c in no_servidor if c not in vistos) if tem_saidas else []
    dados_no_servidor = sum(1 for p in Path(dados).iterdir() if p.is_file() and dado_permitido(p.name)) \
        if Path(dados).is_dir() else 0
    return {"faltam": faltam, "ausentes": ausentes, "sobrando": sobrando, "invalidos": invalidos,
            "no_servidor": len(no_servidor) + dados_no_servidor}


def apagar_sobrando(sobrando: list[str], saidas: Path, dados: Path) -> list[str]:
    """Apaga só arquivos de saidas/reserva (e a pasta que ficar vazia)."""
    apagados = []
    for caminho in sobrando:
        try:
            alvo = destino(caminho, saidas, dados)
        except CaminhoInvalido:
            continue
        if not caminho.startswith("saidas/reserva/"):
            continue
        try:
            alvo.unlink()
            apagados.append(caminho)
        except FileNotFoundError:
            continue
        try:
            alvo.parent.rmdir()  # só se ficou vazia
        except OSError:
            pass
    return apagados


async def receber(fluxo, alvo: Path, tamanho: int, mtime_ns: int, sha256: str) -> None:
    """Grava o corpo do pedido (iterador assíncrono de bytes) em 'alvo', de
    forma atômica, conferindo tamanho e sha256 e preservando o mtime."""
    sha256 = (sha256 or "").lower()
    if not _SHA256.fullmatch(sha256):
        raise ArquivoRecusado("sha256 inválido")
    if not 0 <= tamanho <= LIMITE_ARQUIVO:
        raise CorpoGrandeDemais(f"arquivo acima do limite de {LIMITE_ARQUIVO // 2**20} MB")
    alvo.parent.mkdir(parents=True, exist_ok=True)
    temporario = alvo.with_name(f".{alvo.name}.{secrets.token_hex(4)}{SUFIXO_PARCIAL}")
    resumo, recebidos = hashlib.sha256(), 0
    try:
        with open(temporario, "wb") as f:
            async for pedaco in fluxo:
                recebidos += len(pedaco)
                if recebidos > tamanho or recebidos > LIMITE_ARQUIVO:
                    raise CorpoGrandeDemais("corpo maior que o tamanho declarado")
                resumo.update(pedaco)
                f.write(pedaco)
        if recebidos != tamanho:
            raise ArquivoRecusado("corpo menor que o tamanho declarado")
        if resumo.hexdigest() != sha256:
            raise ArquivoRecusado("sha256 não confere")
        os.utime(temporario, ns=(mtime_ns, mtime_ns))
        for _ in range(40):  # Windows: troca recusada enquanto o site lê o arquivo
            try:
                os.replace(temporario, alvo)
                break
            except PermissionError:
                time.sleep(0.05)
        else:
            raise ArquivoRecusado("arquivo em uso no servidor; tente de novo")
    except BaseException:
        temporario.unlink(missing_ok=True)
        raise
