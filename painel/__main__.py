"""python -m painel [servir | criar-usuario --login L --nome "N" --perfil P | ia-liberar | ...]

Sem argumentos = servir, em 127.0.0.1:8765. No servidor, SECRETARIA_HOST /
SECRETARIA_PORTA / SECRETARIA_PROXY_CONFIAVEL mudam isso (ver config.py: fora
do loopback, só atrás do proxy). A senha do usuário novo é pedida com getpass e
nunca passa por argumento - argumento fica no histórico do terminal.
"""
from __future__ import annotations

import argparse
import hashlib
import secrets
import sqlite3
import sys
from pathlib import Path
from getpass import getpass

from . import auth
from . import config as config_mod
from .banco import Banco


def servir() -> None:
    import uvicorn

    from .app import criar_app

    try:
        host, porta = config_mod.endereco_de_escuta()
        proxies = config_mod.proxies_confiaveis()
    except SystemExit as exc:  # explica no log (painel_servidor.pyw manda o stderr para lá)
        print(f"Painel NÃO subiu: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    print(f"Painel em http://{host}:{porta}  (Ctrl+C para parar)"
          + (f" - atrás do proxy {', '.join(proxies)}" if proxies else ""))
    aviso = config_mod.aviso_de_rede(host, porta)
    if aviso:
        print(aviso, file=sys.stderr)
    # access_log=False: a busca (q) e os ids das reservas vão na URL, e não
    # devem parar em log nenhum. Sem proxy configurado, X-Forwarded-For é
    # ignorado (proxy_headers=False), como sempre foi no PC do João.
    uvicorn.run(criar_app(), host=host, port=porta,
                proxy_headers=bool(proxies), forwarded_allow_ips=proxies or None,
                server_header=False, access_log=False)


def criar_usuario(login: str, nome: str, perfil: str) -> None:
    senha = getpass("Senha: ")
    if senha != getpass("Repita a senha: "):
        raise SystemExit("As senhas não conferem.")
    cfg = config_mod.carregar()
    try:
        auth.criar_usuario(Banco(cfg.db), login, nome, perfil, senha)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print(f"Usuário {login.strip().lower()!r} criado ({perfil}).")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m painel")
    sub = parser.add_subparsers(dest="comando")
    novo = sub.add_parser("criar-usuario", help="cria um usuário (senha pedida no terminal)")
    novo.add_argument("--login", required=True)
    novo.add_argument("--nome", required=True)
    novo.add_argument("--perfil", required=True, choices=auth.PERFIS)
    sub.add_parser("servir", help="sobe o painel em 127.0.0.1:8765")
    sub.add_parser("chave-executor", help="gera a chave do executor do PC e o hash para o servidor")
    copiar = sub.add_parser("copiar-banco", help="cópia consistente do painel.db (API de backup) + integrity_check")
    copiar.add_argument("--para", required=True)
    conferir = sub.add_parser("conferir-banco", help="PRAGMA integrity_check do painel.db (ou de --arquivo)")
    conferir.add_argument("--arquivo")
    sub.add_parser("congelar-pc", help="virada: desabilita as tarefas do PC, encerra site e executor e confere")
    descongelar = sub.add_parser("descongelar-pc", help="virada: habilita de novo Executor, Funil e Varredura")
    descongelar.add_argument("--com-painel", action="store_true", help="também o site do PC (desfazer a virada)")
    sub.add_parser("ia-liberar", help="no PC: desarma o disjuntor da IA (depois de recarregar o saldo ou subir o limite)")
    args = parser.parse_args(argv)

    if args.comando == "criar-usuario":
        criar_usuario(args.login, args.nome, args.perfil)
    elif args.comando == "copiar-banco":
        from . import backup as backup_mod
        try:
            resultado = backup_mod.copiar_banco(config_mod.carregar().db, Path(args.para))
        except FileExistsError:
            raise SystemExit(f"{args.para} já existe - escolha outro nome.")
        print(f"cópia em {args.para}: integrity_check {resultado}")
        if resultado != "ok":
            raise SystemExit(1)
    elif args.comando == "conferir-banco":
        from . import backup as backup_mod
        alvo = Path(args.arquivo) if args.arquivo else config_mod.carregar().db
        try:
            resultado = backup_mod.integridade(alvo)
        except sqlite3.DatabaseError as exc:  # nem abre: "file is not a database", "malformed"
            resultado = f"CORROMPIDO ({exc})"
        print(f"{alvo}: integrity_check {resultado}")
        if resultado != "ok":
            raise SystemExit(1)
    elif args.comando == "congelar-pc":
        from . import congelar
        if not congelar.congelar(config_mod.carregar().db):
            raise SystemExit(1)
    elif args.comando == "descongelar-pc":
        from . import congelar
        if not congelar.descongelar(com_painel=args.com_painel):
            raise SystemExit(1)
    elif args.comando == "ia-liberar":
        from . import ia_falhas
        try:
            anterior = ia_falhas.liberar(ia_falhas.ARQUIVO_NO_PC)
        except OSError as exc:
            raise SystemExit(f"Disjuntor da IA NÃO liberado: {exc}")
        if anterior.get("estado") in ("armado", "ilegivel"):
            print(f"Disjuntor da IA liberado ({anterior.get('estado')} desde {anterior.get('desde') or '?'}: "
                  f"{anterior.get('motivo')}). "
                  f"A próxima rodada lê de novo os documentos que ficaram aguardando leitura.")
        else:
            print("O disjuntor da IA não estava armado; nada a liberar.")
    elif args.comando == "chave-executor":
        chave = secrets.token_urlsafe(32)
        print("No PC de operação (executor e sincronização):")
        print(f"  SECRETARIA_EXECUTOR_CHAVE={chave}")
        print("No servidor (serviço do painel) - só o hash:")
        print(f"  SECRETARIA_EXECUTOR_CHAVE_HASH={hashlib.sha256(chave.encode('utf-8')).hexdigest()}")
    else:
        servir()


if __name__ == "__main__":
    main(sys.argv[1:])
