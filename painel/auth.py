"""Login, senha e sessão do painel. Só biblioteca padrão.

- Senha: PBKDF2-HMAC-SHA256, 390 mil iterações, sal aleatório por usuário,
  comparação em tempo constante.
- Sessão: token aleatório no cookie 'sv_sessao' (HttpOnly, SameSite=Lax, 12 h);
  no banco fica só o SHA-256 dele.
- CSRF: um token por sessão, exigido em todo POST autenticado; no login, o
  cookie pré-login 'sv_pre' faz esse papel.
- Limite: 5 falhas por login ou por IP em 15 min bloqueiam (no SQLite).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from datetime import timedelta
from urllib.parse import urlsplit

from .banco import Banco, agora, de_texto

ITERACOES = 390_000
COOKIE_SESSAO = "sv_sessao"
DURACAO_SESSAO = timedelta(hours=12)
PERFIS = ("admin", "gerente", "registro")
TAMANHO_MINIMO_SENHA = 8
TAMANHO_MAXIMO_LOGIN = 100

# Cookie pré-login (CSRF do próprio formulário de login): emitido no GET
# /login, o POST precisa devolver o mesmo valor no campo 'csrf'. Assinado com o
# segredo do banco e com a hora de emissão dentro, para a validade de 15 min
# valer no servidor e não só no navegador.
COOKIE_PRE_LOGIN = "sv_pre"
DURACAO_PRE_LOGIN = timedelta(minutes=15)

# 5 falhas por login OU por IP em 15 min bloqueiam. Tentativa durante o
# bloqueio também conta como falha: o bloqueio só termina depois de 15 min
# sem tentativa errada.
MAXIMO_FALHAS = 5
JANELA_FALHAS = timedelta(minutes=15)

# Hash de uma senha qualquer, para gastar o mesmo tempo quando o login não
# existe - sem isso, a demora da resposta diria quais logins são válidos.
_SAL_FALSO = secrets.token_hex(16)


def gerar_hash(senha: str, sal: str | None = None, iteracoes: int = ITERACOES) -> tuple[str, str, int]:
    sal = sal or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(sal), iteracoes)
    return h.hex(), sal, iteracoes


def senha_confere(senha: str, hash_: str, sal: str, iteracoes: int) -> bool:
    calculado, _, _ = gerar_hash(senha, sal, iteracoes)
    return hmac.compare_digest(calculado, hash_)


RE_LOGIN = re.compile(r"[a-z0-9._-]{1,%d}" % TAMANHO_MAXIMO_LOGIN)


class LoginRepetido(ValueError):
    pass


def validar_senha(senha: str | None) -> None:
    """A política única de senha (terminal, admin e "trocar minha senha")."""
    if len(senha or "") < TAMANHO_MINIMO_SENHA:
        raise ValueError(f"a senha precisa ter pelo menos {TAMANHO_MINIMO_SENHA} caracteres")


def criar_usuario(banco: Banco, login: str, nome: str, perfil: str, senha: str,
                  por: str = "terminal", trocar_senha: bool = False) -> None:
    """Terminal (python -m painel criar-usuario) e tela de admin. Do admin vem
    trocar_senha=True: quem definiu a senha não é o dono dela."""
    login = (login or "").strip().lower()
    nome = (nome or "").strip()
    if not login or not nome:
        raise ValueError("login e nome são obrigatórios")
    if not RE_LOGIN.fullmatch(login):
        raise ValueError("login só com letras sem acento, números, ponto, hífen ou sublinhado")
    if perfil not in PERFIS:
        raise ValueError(f"perfil precisa ser um de: {', '.join(PERFIS)}")
    validar_senha(senha)
    if banco.buscar_usuario(login) is not None:
        raise LoginRepetido(f"o login {login!r} já existe")
    hash_, sal, iteracoes = gerar_hash(senha)
    try:
        banco.criar_usuario(login, nome, perfil, hash_, sal, iteracoes, por=por, trocar_senha=trocar_senha)
    except sqlite3.IntegrityError:  # outro pedido criou o mesmo login agora
        raise LoginRepetido(f"o login {login!r} já existe") from None


def redefinir_senha(banco: Banco, login: str, senha: str, por: str) -> None:
    """O admin define a senha de outra pessoa: troca obrigatória no próximo
    login, e as sessões abertas dela caem."""
    validar_senha(senha)
    hash_, sal, iteracoes = gerar_hash(senha)
    banco.definir_senha(login, hash_, sal, iteracoes, por=por, trocar_senha=True)


def trocar_propria_senha(banco: Banco, login: str, atual: str, nova: str, token_atual: str | None) -> None:
    """A pessoa troca a própria senha (confere a atual). As outras sessões
    dela caem; a desta aba continua."""
    usuario = banco.buscar_usuario(login)
    if usuario is None or not senha_confere(atual or "", usuario["hash"], usuario["sal"], usuario["iteracoes"]):
        raise ValueError("a senha atual não confere")
    validar_senha(nova)
    if nova == atual:
        raise ValueError("a senha nova precisa ser diferente da atual")
    hash_, sal, iteracoes = gerar_hash(nova)
    banco.definir_senha(login, hash_, sal, iteracoes, por=login, trocar_senha=False,
                        manter_sessao=_hash_token(token_atual) if token_atual else None)


def autenticar(banco: Banco, login: str, senha: str) -> dict | None:
    """Usuário ativo com essa senha, ou None. Mesmo custo para login inexistente."""
    usuario = banco.buscar_usuario((login or "").strip().lower())
    if usuario is None or not usuario["ativo"]:
        gerar_hash(senha or "", _SAL_FALSO)
        return None
    if not senha_confere(senha or "", usuario["hash"], usuario["sal"], usuario["iteracoes"]):
        return None
    return {"login": usuario["login"], "nome": usuario["nome"], "perfil": usuario["perfil"],
            "trocar_senha": bool(usuario.get("trocar_senha"))}


def autenticar_com_limite(banco: Banco, login: str, senha: str, ip: str) -> dict | None:
    """autenticar() com o limite de tentativas. Bloqueado responde igual a
    senha errada - mesma mensagem e o mesmo custo de PBKDF2 -, para o bloqueio
    não confirmar que o login existe."""
    login_normalizado = (login or "").strip().lower()
    chave = login_normalizado[:TAMANHO_MAXIMO_LOGIN]
    ip = (ip or "?")[:64]
    quando = agora()
    falhas_login, falhas_ip = banco.falhas_recentes(chave, ip, quando - JANELA_FALHAS)
    if falhas_login >= MAXIMO_FALHAS or falhas_ip >= MAXIMO_FALHAS:
        gerar_hash(senha or "", _SAL_FALSO)
        banco.registrar_falha(chave, ip, quando)
        return None
    usuario = None
    if len(login_normalizado) <= TAMANHO_MAXIMO_LOGIN:
        usuario = autenticar(banco, login_normalizado, senha)
    else:
        gerar_hash(senha or "", _SAL_FALSO)
    if usuario is None:
        banco.registrar_falha(chave, ip, quando)
        return None
    banco.zerar_falhas_do_login(chave)
    return usuario


def emitir_pre_login(segredo: bytes) -> str:
    base = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    return f"{base}.{_assinar(segredo, base)}"


def _idade_do_pre_login(segredo: bytes, token: str | None) -> float | None:
    """Segundos desde a emissão, ou None se o token não é nosso / está malformado."""
    if not token or len(token) > 200:
        return None
    partes = token.split(".")
    if len(partes) != 3:
        return None
    emitido, aleatorio, assinatura = partes
    if not hmac.compare_digest(assinatura, _assinar(segredo, f"{emitido}.{aleatorio}")):
        return None
    try:
        return time.time() - int(emitido)
    except ValueError:
        return None


def pre_login_valido(segredo: bytes, do_cookie: str | None, do_formulario: str | None) -> bool:
    """O formulário devolveu o mesmo valor do cookie, a assinatura confere e
    ele tem menos de 15 min."""
    if not do_cookie or not do_formulario:
        return False
    if not hmac.compare_digest(do_cookie.encode("utf-8"), do_formulario.encode("utf-8")):
        return False
    idade = _idade_do_pre_login(segredo, do_cookie)
    return idade is not None and 0 <= idade <= DURACAO_PRE_LOGIN.total_seconds()


def pre_login_ainda_vale(segredo: bytes, token: str | None, folga_s: int = 300) -> bool:
    """Dá para reaproveitar este pré-login numa página de login nova? Só se
    ainda sobram pelo menos `folga_s` segundos para a pessoa digitar."""
    idade = _idade_do_pre_login(segredo, token)
    return idade is not None and 0 <= idade <= DURACAO_PRE_LOGIN.total_seconds() - folga_s


def _assinar(segredo: bytes, texto: str) -> str:
    return hmac.new(segredo, texto.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def abrir_sessao(banco: Banco, login: str) -> tuple[str, str]:
    """Devolve (token do cookie, token CSRF)."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    criado = agora()
    banco.criar_sessao(_hash_token(token), login, csrf, criado, criado + DURACAO_SESSAO)
    return token, csrf


def sessao_valida(banco: Banco, token: str | None) -> dict | None:
    """{login, nome, perfil, csrf} da sessão viva, ou None."""
    if not token or len(token) > 200:
        return None
    sessao = banco.buscar_sessao(_hash_token(token))
    if sessao is None or not sessao["ativo"]:
        return None
    if de_texto(sessao["expira_em"]) <= agora():
        banco.apagar_sessao(_hash_token(token))
        return None
    return {"login": sessao["login"], "nome": sessao["nome"], "perfil": sessao["perfil"],
            "csrf": sessao["csrf"], "trocar_senha": bool(sessao["trocar_senha"])}


def encerrar_sessao(banco: Banco, token: str | None) -> None:
    if token:
        banco.apagar_sessao(_hash_token(token))


def csrf_confere(esperado: str | None, recebido: str | None) -> bool:
    if not esperado or not recebido:
        return False
    return hmac.compare_digest(esperado.encode("utf-8"), recebido.encode("utf-8"))


def destino_seguro(proximo: str | None) -> str:
    """Só caminho interno: '/algo'. Bloqueia '//host', '/\\host', esquema e
    host - senão o login viraria redirecionador para site de fora."""
    proximo = (proximo or "").strip()
    if not proximo.startswith("/") or proximo.startswith("//") or "\\" in proximo:
        return "/"
    if any(ord(c) < 32 for c in proximo):
        return "/"
    partes = urlsplit(proximo)
    if partes.scheme or partes.netloc:
        return "/"
    if partes.path.startswith("/login"):
        return "/"
    return proximo
