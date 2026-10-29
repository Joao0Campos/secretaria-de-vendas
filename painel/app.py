"""Rotas do painel (FastAPI).

Toda rota exige login, menos /login e /static. Sem sessão: página vai para
/login?next=... (303); rota /api/ responde 401, porque quem chama é o
JavaScript da página e um redirecionamento ali só esconderia o erro.

Formulário é lido à mão (urllib): o .venv do projeto não tem python-multipart,
e não vale instalar uma dependência para dois campos.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import auth
from . import backup as backup_mod
from . import como_conferir as como_conferir_mod
from . import consumo as consumo_mod
from . import config as config_mod
from . import funil as funil_mod
from . import ia_falhas
from . import revisao as revisao_mod
from . import sincronia
from .banco import (
    PEDIDO_CONCLUIDO,
    PEDIDO_EM_ANDAMENTO,
    PEDIDO_FALHOU,
    PEDIDO_FORA_DO_FUNIL,
    PEDIDO_NA_FILA,
    PEDIDO_TIPO_FUNIL,
    PEDIDOS_ABERTOS,
    Banco,
    BuscaRecente,
    FilaCheia,
    ReanaliseRecente,
    ReservaInexistente,
    de_texto,
)
from .dados import (
    TIPO_RESERVA,
    TIPOS,
    Repositorio,
    arquivo_pelo_token,
    caminho_seguro_do_arquivo,
    filtrar,
    filtro_de,
    montar_apont,
    montar_detalhe,
    montar_item,
    opcoes,
    totais,
)
from .exportar import gerar_xlsx, gerar_xlsx_revisao
from .metricas import calcular, periodo_de

PASTA_PAINEL = Path(__file__).resolve().parent
TAMANHO_MAXIMO_CORPO = 64 * 1024
RE_EXTENSAO = re.compile(r"\.[a-z0-9]{1,5}")

# Abrem no navegador. O resto (inclusive HTML e SVG, que rodariam script na
# origem do painel) vai como download.
TIPOS_INLINE = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}

CABECALHOS_SEGURANCA = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "same-origin",
}

# Em toda resposta HTML. 'unsafe-inline' só em style, por causa dos style=
# dos templates; script só do próprio painel.
CSP = ("default-src 'self'; script-src 'self'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'self'; "
       "form-action 'self'; base-uri 'none'")
HSTS = "max-age=31536000"


def _rota_publica(caminho: str) -> bool:
    # /favicon.ico é pedido sozinho pelo Chrome logo depois da página de login.
    # Protegido, ele era redirecionado para /login, que emitia um cookie
    # pré-login novo e invalidava o formulário aberto (13/10/2026: "A página de
    # login expirou" em toda tentativa no Chrome do João).
    # /api/executor/ e /api/sync/: sem sessão; cada rota exige a chave do executor.
    return (caminho in ("/login", "/favicon.ico") or caminho == "/static"
            or caminho.startswith(("/static/", "/api/executor/", "/api/sync/")))


# Opção B: sem estado do executor do PC há mais que isto = "executor desligado".
EXECUTOR_DESLIGADO_APOS_S = 60
# Banner da IA (só admin): estado conferido há mais que isto = "pode estar desatualizado".
IA_ESTADO_VELHO_APOS_S = 10 * 60


# Senha definida pelo admin: até trocar, a pessoa só vê a tela de troca (e sai).
ROTAS_COM_SENHA_A_TROCAR = ("/trocar-senha", "/logout")

# Mensagens da tela de admin depois de uma ação que deu certo (?feito=...).
FEITOS_DO_ADMIN = {
    "criado": "Usuário criado. Ele troca a senha no primeiro login.",
    "desativado": "Usuário desativado. As sessões dele foram encerradas.",
    "reativado": "Usuário reativado.",
    "senha": "Senha redefinida. As sessões dele foram encerradas e ele troca a senha no próximo login.",
    "perfil": "Perfil alterado.",
}


def _ultima_varredura(pasta: Path) -> dict | None:
    """Resumo da última rodada de varredura_diaria.py (varreduras.jsonl, ao lado
    do banco), para o topo das páginas dizer se os dados são de hoje. Rodada só
    de listagem (--so-listar) não conta: não analisou nada."""
    try:
        linhas = (pasta / "varreduras.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for linha in reversed(linhas):
        try:
            r = json.loads(linha)
            inicio = datetime.strptime(r["inicio"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError, TypeError):
            continue
        if r.get("resultado") == "so_listagem":
            continue
        return {
            "quando": inicio.strftime("%d/%m %H:%M"),
            "hoje": inicio.date() == datetime.now().date(),
            "resultado": r.get("resultado"),
            "reservas": f"{r.get('reservas_ok', 0)}/{r.get('reservas_alvo', 0)}",
            "falhas": len(r.get("falhas") or []),
        }
    return None


def _data_da_ultima_varredura(pasta: Path):
    """Dia da última rodada da varredura diária que analisou algo (ignora as
    só de listagem e as que pararam com erro antes de analisar)."""
    try:
        linhas = (pasta / "varreduras.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for linha in reversed(linhas):
        try:
            r = json.loads(linha)
            if r.get("resultado") in ("ok", "ok_com_falhas") and r.get("reservas_ok"):
                return datetime.strptime(r["inicio"], "%Y-%m-%d %H:%M:%S").date()
        except (ValueError, KeyError, TypeError):
            continue
    return None


# Por que um pedido está esperando, pelo dono da trava (painel_dados/varredura.trava,
# o "modo" que varredura_diaria._pegar_trava() grava nela).
AGUARDANDO_POR_MODO = {
    "varredura": "aguardando a varredura diária terminar",
    "repescagem": "aguardando a varredura diária terminar",
    "funil": "aguardando a atualização do funil (segundos)",
    "reanalise": "aguardando outra reanálise terminar",
    "releitura": "aguardando a releitura das Telas terminar",
}
AGUARDANDO_EXECUTOR = "aguardando o executor de reanálise"


def _dono_da_trava(pasta: Path) -> dict | None:
    """{"modo", "desde"} de quem está com a trava, ou None. Só lê o arquivo: o
    site não pega nem solta a trava."""
    try:
        info = json.loads((pasta / "varredura.trava").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return {"modo": None, "desde": None}
    return {"modo": info.get("modo"), "desde": str(info.get("inicio") or "")[:19] or None}


def _estado_da_ia(cfg, banco) -> dict | None:
    """O banner do admin (ver painel/ia_falhas.py). No PC o ia_estado.json é o
    próprio estado, lido agora. No servidor (opção B), o executor manda o estado
    a cada sinal de vida; sem sinal recente vale o mais novo entre o último dele e
    a cópia que veio pelo envio, sempre com "pode estar desatualizado"."""
    copia = ia_falhas.estado(cfg.db.parent / ia_falhas.NOME_DO_ARQUIVO)
    if not cfg.executor_chave_hash:
        return ia_falhas.para_o_banner(copia, datetime.now(), IA_ESTADO_VELHO_APOS_S)
    executor = banco.estado_do_executor() or {}
    visto = de_texto(executor["visto_em"]) if executor.get("visto_em") else None
    try:
        do_executor = json.loads(executor["ia"]) if executor.get("ia") else None
    except ValueError:
        do_executor = None
    if (do_executor is not None and visto
            and (datetime.now() - visto).total_seconds() <= IA_ESTADO_VELHO_APOS_S):
        return ia_falhas.para_o_banner(do_executor or None, visto, IA_ESTADO_VELHO_APOS_S)
    candidatos = [(e, v) for e, v in ((ia_falhas.limpar_estado(do_executor), visto),
                                      (ia_falhas.limpar_estado(copia), None)) if e]
    if not candidatos:
        return None
    estado, quando = max(candidatos, key=lambda c: c[0].get("atualizado_em") or "")
    return ia_falhas.para_o_banner(estado, quando, IA_ESTADO_VELHO_APOS_S)


def _e_navegacao(request: Request) -> bool:
    """Pedido de página (o usuário navegando) e não de recurso (ícone, imagem,
    fetch). Só navegação é mandada para o login; o resto recebe 401.
    Navegadores atuais dizem isso em Sec-Fetch-Dest; sem ele, só pedido
    explícito de imagem deixa de ser tratado como navegação."""
    destino = (request.headers.get("sec-fetch-dest") or "").lower()
    if destino:
        return destino in ("document", "frame", "iframe")
    return not (request.headers.get("accept") or "").lower().startswith("image/")


def criar_app(cfg: config_mod.Config | None = None, templates_dir: Path | None = None) -> FastAPI:
    cfg = cfg or config_mod.carregar()
    banco = Banco(cfg.db)
    repo = Repositorio(cfg.saidas, banco)
    listagem = funil_mod.Listagem(cfg.db.parent)
    modelos_cv = {TIPO_RESERVA: cfg.cv_url_reserva}
    templates = Jinja2Templates(directory=str(templates_dir or PASTA_PAINEL / "templates"))
    templates.env.globals["como_conferir"] = como_conferir_mod.como_conferir

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(_app):
        # Backup diário do painel.db pelo próprio serviço (ver painel/backup.py).
        agendador = backup_mod.Agendador(cfg.db) if cfg.backup_diario else None
        if agendador:
            agendador.iniciar()
        try:
            yield
        finally:
            if agendador:
                agendador.parar()

    app = FastAPI(title=config_mod.APP_NOME, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=ciclo_de_vida)
    app.state.cfg = cfg
    app.state.banco = banco
    app.state.repo = repo
    app.mount("/static", StaticFiles(directory=str(PASTA_PAINEL / "static"), check_dir=False), name="static")

    # ------------------------------------------------------------ login obrigatório
    @app.middleware("http")
    async def exigir_login(request: Request, call_next):
        caminho = request.url.path
        request.state.usuario = None
        if not _rota_publica(caminho):
            usuario = await run_in_threadpool(
                auth.sessao_valida, banco, request.cookies.get(auth.COOKIE_SESSAO))
            if usuario is None:
                if caminho.startswith("/api/") or not _e_navegacao(request):
                    resposta = JSONResponse({"detail": "login necessário"}, status_code=401)
                else:
                    destino = caminho + (f"?{request.url.query}" if request.url.query else "")
                    resposta = RedirectResponse(f"/login?next={quote(destino, safe='')}", status_code=303)
            elif usuario["trocar_senha"] and caminho not in ROTAS_COM_SENHA_A_TROCAR:
                if caminho.startswith("/api/") or not _e_navegacao(request):
                    resposta = JSONResponse({"detail": "troque a senha antes de continuar"}, status_code=403)
                else:
                    resposta = RedirectResponse("/trocar-senha", status_code=303)
            else:
                request.state.usuario = usuario
                resposta = await call_next(request)
        else:
            resposta = await call_next(request)
        for nome, valor in CABECALHOS_SEGURANCA.items():
            resposta.headers.setdefault(nome, valor)
        if not caminho.startswith("/static"):
            resposta.headers.setdefault("Cache-Control", "no-store")
        if resposta.headers.get("content-type", "").startswith("text/html"):
            resposta.headers.setdefault("Content-Security-Policy", CSP)
        if cfg.cookie_seguro:
            resposta.headers.setdefault("Strict-Transport-Security", HSTS)
        return resposta

    # ------------------------------------------------------------ utilidades
    def contexto(request: Request, **extra) -> dict:
        u = request.state.usuario
        return {
            "app_nome": config_mod.APP_NOME,
            "usuario": {"login": u["login"], "nome": u["nome"], "perfil": u["perfil"]} if u else None,
            "e_admin": bool(u and u["perfil"] == "admin"),
            # Só o admin vê o estado do backup do painel.db (falha ou disco cheio não ficam só no log).
            "backup": (backup_mod.estado(cfg.db.parent)
                       if cfg.backup_diario and u and u["perfil"] == "admin" else None),
            # Só o admin vê o banner da IA sem saldo/limite (o time vê o selo de cada reserva).
            "ia": _estado_da_ia(cfg, banco) if u and u["perfil"] == "admin" else None,
            "mostrar_impacto": cfg.mostrar_impacto,
            "csrf": u["csrf"] if u else None,
            "varredura": _ultima_varredura(cfg.db.parent) if u else None,
            "funil": funil_mod.resumo_do_funil(listagem) if u else None,
            **extra,
        }

    def csrf_ok(request: Request, do_formulario: str | None = None) -> bool:
        recebido = request.headers.get("x-csrf-token") or do_formulario
        return auth.csrf_confere((request.state.usuario or {}).get("csrf"), recebido)

    async def ler_corpo(request: Request, teto: int = TAMANHO_MAXIMO_CORPO) -> bytes:
        """Em pedaços, com teto rígido: sem Content-Length, request.body()
        carregaria tudo antes de conferir."""
        if int(request.headers.get("content-length") or 0) > teto:
            raise HTTPException(413, "corpo grande demais")
        try:
            return await sincronia.ler_com_teto(request.stream(), teto)
        except sincronia.CorpoGrandeDemais:
            raise HTTPException(413, "corpo grande demais")

    async def ler_formulario(request: Request) -> dict[str, str]:
        corpo = await ler_corpo(request)
        tipo = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if tipo != "application/x-www-form-urlencoded":
            return {}
        try:
            campos = parse_qs(corpo.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        except (UnicodeDecodeError, ValueError):
            return {}
        return {k: v[0] for k, v in campos.items()}

    def marcar_funil(item: dict) -> dict:
        """'saiu_do_funil': a situação na listagem do CV, se ela já está fora das
        etapas da varredura (segundo a listagem - a reanálise confirma)."""
        if item.get("tipo") == TIPO_RESERVA:
            situacao = listagem.situacao(item["id"])
            item["situacao_listagem"] = situacao
            item["saiu_do_funil"] = situacao if situacao and not funil_mod.no_funil(situacao) else None
            # Só no funil: o executor marcaria fora_do_funil sem analisar (revisão da Forja).
            item["pode_reanalisar"] = item["id"] in listagem.ids() and funil_mod.no_funil(situacao)
        return item

    def carregar_tudo():
        reservas = repo.atualizar()
        status = banco.status_atuais()
        pares = [(r, marcar_funil(montar_item(r, status, modelos_cv))) for r in reservas]
        return reservas, status, pares

    def lista(params) -> dict:
        _, status, pares = carregar_tudo()
        filtro = filtro_de(params)
        # Decisão do João (13/10/2026): a lista mostra só o que a última
        # varredura diária leu - reserva que saiu do funil continua guardada
        # (conta no Impacto), mas fica em "anteriores" para não confundir.
        dia = _data_da_ultima_varredura(cfg.db.parent)
        analise = (params.get("analise") or "").strip()
        filtro["analise"] = analise if analise in ("anteriores", "todas") else ("ultima" if dia else "todas")
        if dia and filtro["analise"] != "todas":
            de_hoje = dia.strftime("%d/%m/%Y")
            querer = filtro["analise"] == "ultima"
            pares = [(r, i) for r, i in pares if (str(i.get("analisado_em") or "")[:10] == de_hoje) == querer]
        filtro["dia_da_varredura"] = dia.strftime("%d/%m") if dia else None
        filtrados = filtrar(pares, filtro)
        itens = [item for _, item in filtrados]
        # No funil (segundo a listagem) e ainda sem planilha: botão "Analisar agora".
        sem_analise = listagem.no_funil_sem_analise({r.id for r, _ in pares})
        return {"itens": itens, "totais": totais(itens), "filtro": filtro,
                "opcoes": opcoes([item for _, item in pares]), "no_funil_sem_analise": sem_analise,
                "_filtrados": filtrados, "_status": status}

    def detalhe(tipo: str, id_: int) -> dict:
        if tipo not in TIPOS:
            raise HTTPException(404)
        _, status, pares = carregar_tudo()
        for r, _item in pares:
            if r.tipo == tipo and r.id == id_:
                return marcar_funil(montar_detalhe(r, status, modelos_cv))
        raise HTTPException(404)

    def impacto(periodo_bruto) -> dict:
        if not cfg.mostrar_impacto:  # oculta desde 20/10/2026 (config.MOSTRAR_IMPACTO_PADRAO)
            raise HTTPException(404)
        periodo = periodo_de(periodo_bruto)
        reservas, _, _ = carregar_tudo()
        m = calcular(reservas, banco.listar_apontamentos(TIPO_RESERVA), banco.listar_eventos(TIPO_RESERVA),
                     periodo, cfg.minutos_manuais_por_reserva)
        return {"m": m, "periodo": periodo}

    def revisao(params) -> dict:
        reservas = repo.atualizar()
        return revisao_mod.montar(reservas, banco.listar_apontamentos(TIPO_RESERVA),
                                  banco.listar_eventos(TIPO_RESERVA),
                                  revisao_mod.filtro_de(params), modelos_cv=modelos_cv)

    def publico(dados: dict) -> dict:
        return {k: v for k, v in dados.items() if not k.startswith("_")}

    # ------------------------------------------------------------ login / logout
    def pagina_de_login(request: Request, destino: str, erro: str | None, status: int = 200):
        """A página de login sai com o cookie pré-login, e o mesmo valor vai para
        o formulário na variável 'csrf'. Se o navegador já tem um que ainda vale
        por uns minutos, ele é reaproveitado: um GET /login extra (outra aba, um
        pedido automático do navegador) não pode invalidar o formulário aberto."""
        atual = request.cookies.get(auth.COOKIE_PRE_LOGIN)
        pre = atual if auth.pre_login_ainda_vale(banco.segredo, atual) else auth.emitir_pre_login(banco.segredo)
        resposta = templates.TemplateResponse(
            request, "login.html", contexto(request, erro=erro, next=destino, csrf=pre), status_code=status)
        resposta.set_cookie(auth.COOKIE_PRE_LOGIN, pre, max_age=int(auth.DURACAO_PRE_LOGIN.total_seconds()),
                            httponly=True, samesite="lax", secure=cfg.cookie_seguro, path="/")
        # Formulário de login guardado em cache (voltar, restaurar aba) mandaria
        # um token que não bate mais com o cookie novo.
        resposta.headers["Cache-Control"] = "no-store"
        return resposta

    @app.get("/favicon.ico")
    def favicon():
        # O ícone de verdade é /static/favicon.svg (link nos templates); isto só
        # responde quem pede o caminho padrão, sem passar pelo login.
        return Response(status_code=204)

    @app.get("/login")
    def pagina_login(request: Request, next: str = "/"):
        destino = auth.destino_seguro(next)
        if auth.sessao_valida(banco, request.cookies.get(auth.COOKIE_SESSAO)):
            return RedirectResponse(destino, status_code=303)
        return pagina_de_login(request, destino, None)

    @app.post("/login")
    async def fazer_login(request: Request):
        form = await ler_formulario(request)
        destino = auth.destino_seguro(form.get("next"))
        if not auth.pre_login_valido(banco.segredo, request.cookies.get(auth.COOKIE_PRE_LOGIN), form.get("csrf")):
            return pagina_de_login(request, destino, "A página de login expirou. Tente de novo.", 403)
        ip = request.client.host if request.client else "?"
        usuario = await run_in_threadpool(
            auth.autenticar_com_limite, banco, form.get("login", ""), form.get("senha", ""), ip)
        if usuario is None:
            return pagina_de_login(request, destino, "Login ou senha inválidos.", 401)
        antigo = request.cookies.get(auth.COOKIE_SESSAO)
        await run_in_threadpool(auth.encerrar_sessao, banco, antigo)
        token, _csrf = await run_in_threadpool(auth.abrir_sessao, banco, usuario["login"])
        await run_in_threadpool(banco.registrar_acesso, usuario["login"])
        resposta = RedirectResponse("/trocar-senha" if usuario["trocar_senha"] else destino, status_code=303)
        resposta.set_cookie(auth.COOKIE_SESSAO, token, max_age=int(auth.DURACAO_SESSAO.total_seconds()),
                            httponly=True, samesite="lax", secure=cfg.cookie_seguro, path="/")
        resposta.delete_cookie(auth.COOKIE_PRE_LOGIN, path="/", httponly=True, samesite="lax",
                               secure=cfg.cookie_seguro)
        return resposta

    @app.post("/logout")
    async def fazer_logout(request: Request):
        form = await ler_formulario(request)
        if not csrf_ok(request, form.get("csrf")):
            raise HTTPException(403, "CSRF inválido")
        await run_in_threadpool(auth.encerrar_sessao, banco, request.cookies.get(auth.COOKIE_SESSAO))
        resposta = RedirectResponse("/login", status_code=303)
        resposta.delete_cookie(auth.COOKIE_SESSAO, path="/", httponly=True, samesite="lax",
                               secure=cfg.cookie_seguro)
        return resposta

    # ------------------------------------------------------------ minha senha
    def pagina_trocar_senha(request: Request, erro: str | None = None, status: int = 200):
        u = request.state.usuario
        return templates.TemplateResponse(
            request, "trocar_senha.html",
            contexto(request, secao="senha", erro=erro, obrigatoria=bool(u["trocar_senha"]),
                     minimo=auth.TAMANHO_MINIMO_SENHA, feito=request.query_params.get("feito") == "1"),
            status_code=status)

    @app.get("/trocar-senha")
    def get_trocar_senha(request: Request):
        return pagina_trocar_senha(request)

    @app.post("/trocar-senha")
    async def post_trocar_senha(request: Request):
        form = await ler_formulario(request)
        if not csrf_ok(request, form.get("csrf")):
            raise HTTPException(403, "CSRF inválido")
        if form.get("nova", "") != form.get("confirmacao", ""):
            return pagina_trocar_senha(request, "A confirmação não é igual à senha nova.", 400)
        try:
            await run_in_threadpool(auth.trocar_propria_senha, banco, request.state.usuario["login"],
                                    form.get("atual", ""), form.get("nova", ""),
                                    request.cookies.get(auth.COOKIE_SESSAO))
        except ValueError as exc:
            return pagina_trocar_senha(request, str(exc).capitalize() + ".", 400)
        return RedirectResponse("/trocar-senha?feito=1", status_code=303)

    # ------------------------------------------------------------ admin: usuários
    def exigir_admin(request: Request) -> dict:
        u = request.state.usuario
        if u["perfil"] != "admin":
            raise HTTPException(403, "só o perfil admin")
        return u

    def pagina_admin(request: Request, erro: str | None = None, status: int = 200, form: dict | None = None):
        feito = FEITOS_DO_ADMIN.get(request.query_params.get("feito") or "")
        return templates.TemplateResponse(
            request, "admin_usuarios.html",
            contexto(request, secao="admin", usuarios=banco.listar_usuarios(), eventos=banco.eventos_de_usuarios(),
                     perfis=auth.PERFIS, minimo=auth.TAMANHO_MINIMO_SENHA, erro=erro, feito=None if erro else feito,
                     form=form or {}),
            status_code=status)

    async def form_do_admin(request: Request) -> tuple[dict, dict]:
        u = exigir_admin(request)
        form = await ler_formulario(request)
        if not csrf_ok(request, form.get("csrf")):
            raise HTTPException(403, "CSRF inválido")
        return u, form

    def alvo_existente(login: str) -> dict:
        alvo = banco.buscar_usuario(login)
        if alvo is None:
            raise HTTPException(404, "usuário não encontrado")
        return alvo

    @app.get("/admin/consumo-ia")
    def get_consumo_ia(request: Request):
        """Consumo real da IA (painel_dados/consumo_ia.jsonl, que o PC grava e envia):
        hoje, 7 dias, mês atual e anterior, custo estimado e projeção. Só admin."""
        exigir_admin(request)
        linhas = consumo_mod.ler(cfg.db.parent, consumo_mod.meses_da_tela())  # só os meses que a tela mostra
        return templates.TemplateResponse(
            request, "admin_consumo.html",
            contexto(request, secao="consumo", c=consumo_mod.agregar(linhas), sem_dados=not linhas))

    @app.get("/admin/usuarios")
    def get_admin_usuarios(request: Request):
        exigir_admin(request)
        return pagina_admin(request)

    @app.post("/admin/usuarios")
    async def post_criar_usuario(request: Request):
        u, form = await form_do_admin(request)
        dados = {k: form.get(k, "") for k in ("login", "nome", "perfil")}
        try:
            await run_in_threadpool(auth.criar_usuario, banco, dados["login"], dados["nome"], dados["perfil"],
                                    form.get("senha", ""), u["login"], True)
        except auth.LoginRepetido as exc:
            return pagina_admin(request, str(exc).capitalize() + ".", 409, dados)
        except ValueError as exc:
            return pagina_admin(request, str(exc).capitalize() + ".", 400, dados)
        return RedirectResponse("/admin/usuarios?feito=criado", status_code=303)

    @app.post("/admin/usuarios/{login}/ativo")
    async def post_ativo(request: Request, login: str):
        u, form = await form_do_admin(request)
        alvo_existente(login)
        ativo = form.get("ativo") == "1"
        if login == u["login"] and not ativo:
            return pagina_admin(request, "Você não pode desativar o próprio usuário.", 400)
        await run_in_threadpool(banco.definir_ativo, login, ativo, u["login"])
        return RedirectResponse(f"/admin/usuarios?feito={'reativado' if ativo else 'desativado'}", status_code=303)

    @app.post("/admin/usuarios/{login}/perfil")
    async def post_perfil(request: Request, login: str):
        u, form = await form_do_admin(request)
        alvo_existente(login)
        perfil = form.get("perfil", "")
        if perfil not in auth.PERFIS:
            return pagina_admin(request, "Perfil inválido.", 400)
        if login == u["login"] and perfil != "admin":
            return pagina_admin(request, "Você não pode tirar o próprio perfil admin.", 400)
        await run_in_threadpool(banco.definir_perfil, login, perfil, u["login"])
        return RedirectResponse("/admin/usuarios?feito=perfil", status_code=303)

    @app.post("/admin/usuarios/{login}/senha")
    async def post_redefinir_senha(request: Request, login: str):
        u, form = await form_do_admin(request)
        alvo_existente(login)
        if login == u["login"]:
            return pagina_admin(request, "Para a sua senha, use \"Trocar minha senha\".", 400)
        try:
            await run_in_threadpool(auth.redefinir_senha, banco, login, form.get("senha", ""), u["login"])
        except ValueError as exc:
            return pagina_admin(request, str(exc).capitalize() + ".", 400)
        return RedirectResponse("/admin/usuarios?feito=senha", status_code=303)

    # ------------------------------------------------------------ páginas
    @app.get("/")
    def pagina_reservas(request: Request):
        dados = lista(request.query_params)
        return templates.TemplateResponse(request, "reservas.html", contexto(request, **publico(dados)))

    @app.get("/reserva/{tipo}/{id_reserva}")
    def pagina_reserva(request: Request, tipo: str, id_reserva: int):
        return templates.TemplateResponse(request, "reserva.html",
                                          contexto(request, r=detalhe(tipo, id_reserva)))

    @app.get("/impacto")
    def pagina_impacto(request: Request, periodo: str = ""):
        return templates.TemplateResponse(request, "impacto.html", contexto(request, **impacto(periodo)))

    @app.get("/exportar.xlsx")
    def exportar(request: Request):
        dados = lista(request.query_params)
        detalhes = [montar_detalhe(r, dados["_status"], modelos_cv) for r, _ in dados["_filtrados"]]
        nome = f"secretaria-vendas-{datetime.now():%Y%m%d-%H%M}.xlsx"
        return Response(
            gerar_xlsx(detalhes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{nome}"'})

    @app.get("/revisao")
    def pagina_revisao(request: Request):
        return templates.TemplateResponse(request, "revisao.html",
                                          contexto(request, **revisao(request.query_params)))

    @app.get("/revisao.xlsx")
    def exportar_revisao(request: Request):
        dados = revisao(request.query_params)
        nome = f"revisao-de-regras-{datetime.now():%Y%m%d-%H%M}.xlsx"
        return Response(
            gerar_xlsx_revisao(dados["grupos"]),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{nome}"'})

    # ------------------------------------------------------------ API JSON
    @app.get("/api/reservas")
    def api_reservas(request: Request):
        return publico(lista(request.query_params))

    @app.get("/api/reserva/{tipo}/{id_reserva}")
    def api_reserva(tipo: str, id_reserva: int):
        return detalhe(tipo, id_reserva)

    @app.get("/api/impacto")
    def api_impacto(periodo: str = ""):
        return impacto(periodo)

    @app.get("/api/revisao")
    def api_revisao(request: Request):
        return revisao(request.query_params)

    @app.post("/api/tratativa")
    async def api_tratativa(request: Request):
        if not csrf_ok(request):
            raise HTTPException(403, "CSRF inválido")
        corpo = await ler_corpo(request)
        try:
            pedido = json.loads(corpo or b"{}")
        except ValueError:
            raise HTTPException(400, "JSON inválido")
        if not isinstance(pedido, dict):
            raise HTTPException(400, "JSON inválido")
        chave, status, motivo = pedido.get("chave"), pedido.get("status"), pedido.get("motivo")
        detalhe, veredito = pedido.get("detalhe"), pedido.get("veredito")
        if (not isinstance(chave, str) or not isinstance(status, str)
                or not all(v is None or isinstance(v, str) for v in (motivo, detalhe, veredito))):
            raise HTTPException(400, "campos inválidos")
        motivo = motivo.strip() if motivo else None
        try:
            evento = await run_in_threadpool(
                banco.registrar_tratativa, chave, status, motivo, request.state.usuario["login"], detalhe,
                veredito or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except KeyError:
            raise HTTPException(404, "apontamento não encontrado")
        apontamento = await run_in_threadpool(banco.buscar_apontamento, chave)
        return montar_apont(apontamento, {chave: evento})

    # ------------------------------------------------------------ reanálise
    def motivo_da_espera() -> str:
        if cfg.executor_chave_hash:  # opção B: a trava está no PC; o executor manda o modo dela
            estado = banco.estado_do_executor()
            visto = de_texto(estado["visto_em"]) if estado else None
            if visto is None or (datetime.now() - visto).total_seconds() > EXECUTOR_DESLIGADO_APOS_S:
                return "executor desligado" + (f" (último sinal às {visto:%d/%m %H:%M})" if visto else "")
            return AGUARDANDO_POR_MODO.get(estado["modo"], AGUARDANDO_EXECUTOR)
        dono = _dono_da_trava(cfg.db.parent)
        return (AGUARDANDO_POR_MODO.get((dono or {}).get("modo"), AGUARDANDO_EXECUTOR)
                if dono else AGUARDANDO_EXECUTOR)

    # ------------------------------------------------------------ opção B: executor e envio do PC
    def exigir_chave(request: Request) -> None:
        """Rotas do executor do PC: sem a chave configurada no servidor, não
        existem (404); com ela, só com "Authorization: Bearer <chave>" (401)."""
        if not cfg.executor_chave_hash:
            raise HTTPException(404)
        bruto = request.headers.get("authorization") or ""
        tipo, _, chave = bruto.partition(" ")
        calculado = hashlib.sha256(chave.strip().encode("utf-8")).hexdigest()
        if tipo.lower() != "bearer" or not chave.strip() or not hmac.compare_digest(calculado, cfg.executor_chave_hash):
            raise HTTPException(401, "chave do executor inválida")

    async def json_do_executor(request: Request, limite: int = TAMANHO_MAXIMO_CORPO) -> dict:
        exigir_chave(request)
        corpo = await ler_corpo(request, limite)
        try:
            dados = json.loads(corpo or b"{}")
        except ValueError:
            raise HTTPException(400, "JSON inválido")
        if not isinstance(dados, dict):
            raise HTTPException(400, "JSON precisa ser um objeto")
        return dados

    @app.post("/api/executor/estado")
    async def api_executor_estado(request: Request):
        """Sinal de vida do executor + o modo da trava no PC + o estado da IA no PC
        ("ia", opcional: o disjuntor de painel/ia_falhas.py). Devolve se há
        pedido na fila (o executor só pega a trava do PC quando há)."""
        dados = await json_do_executor(request)
        modo = dados.get("modo")
        ia = None
        if "ia" in dados:
            ia = json.dumps(ia_falhas.limpar_estado(dados["ia"]) or {}, ensure_ascii=False)
        await run_in_threadpool(banco.registrar_estado_do_executor, modo if isinstance(modo, str) else None, ia)
        orfaos = await run_in_threadpool(banco.recuperar_pedidos_orfaos)
        return {"ha_pedido": await run_in_threadpool(banco.ha_pedido_na_fila), "orfaos": orfaos}

    @app.post("/api/executor/proximo")
    async def api_executor_proximo(request: Request):
        await json_do_executor(request)
        return {"pedido": await run_in_threadpool(banco.pegar_proximo_pedido)}

    @app.post("/api/executor/concluir")
    async def api_executor_concluir(request: Request):
        dados = await json_do_executor(request)
        try:
            ok = await run_in_threadpool(banco.concluir_pedido, int(dados["id"]), dados["status"],
                                         dados.get("resultado"), dados.get("erro"))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "pedido ou status inválido")
        if not ok:
            raise HTTPException(409, "o pedido não está mais em andamento")
        return {"ok": True}

    @app.post("/api/executor/enfileirar")
    async def api_executor_enfileirar(request: Request):
        """"Buscar reservas novas": o executor manda as reservas do funil sem
        planilha; o site enfileira com a origem e o autor do pedido de busca e
        confere contra a listagem que o PC acabou de enviar."""
        dados = await json_do_executor(request)
        try:
            busca = banco.buscar_pedido(int(dados["pedido_id"]))
            ids = [int(i) for i in dados["ids"]]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "pedido_id ou ids inválidos")
        if busca is None or busca.get("tipo") != PEDIDO_TIPO_FUNIL or busca["status"] != PEDIDO_EM_ANDAMENTO:
            raise HTTPException(409, "não é uma busca em andamento")
        return await run_in_threadpool(banco.enfileirar_varias, ids, busca["origem"], busca["pedido_por"],
                                       funil_mod.Listagem(cfg.db.parent).ids())

    @app.get("/api/executor/backup")
    def api_executor_backup(request: Request):
        """O backup mais recente do painel.db, para a cópia fora do servidor
        (o PC baixa um por dia). sha256 no cabeçalho X-Backup-Sha256."""
        exigir_chave(request)
        mais_novo = next(iter(backup_mod.backups(cfg.db.parent / "backups")), None)
        if mais_novo is None:
            raise HTTPException(404, "ainda não há backup")
        return FileResponse(mais_novo, media_type="application/octet-stream", filename=mais_novo.name,
                            headers={"X-Backup-Sha256": backup_mod.sha256(mais_novo)})

    async def comparar_manifesto(request: Request) -> tuple[dict, dict]:
        dados = await json_do_executor(request, sincronia.LIMITE_MANIFESTO)
        try:
            return dados, await run_in_threadpool(sincronia.comparar, dados.get("arquivos"), cfg.saidas,
                                                  cfg.db.parent)
        except sincronia.CorpoGrandeDemais as exc:
            raise HTTPException(413, str(exc))
        except sincronia.ArquivoRecusado as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/sync/manifesto")
    async def api_sync_manifesto(request: Request):
        """O PC manda o manifesto; volta o que falta enviar. SÓ calcula: nunca
        apaga nada (as exclusões ficam para /api/sync/concluir)."""
        _, r = await comparar_manifesto(request)
        return r

    @app.post("/api/sync/concluir")
    async def api_sync_concluir(request: Request):
        """Chamada FINAL do PC, depois de todos os envios e da publicação: com
        completo=true e o mesmo manifesto, apaga o que sumiu do PC (só em
        saidas/reserva) - e só se o servidor já tem tudo o que o manifesto diz."""
        dados, r = await comparar_manifesto(request)
        if dados.get("completo") is not True:
            return {"apagados": [], "recusado": "manifesto incompleto"}
        if r["faltam"] or r["invalidos"]:
            return {"apagados": [], "recusado": f"{len(r['faltam'])} arquivo(s) ainda faltam no servidor"}
        return {"apagados": await run_in_threadpool(sincronia.apagar_sobrando, r["sobrando"], cfg.saidas,
                                                    cfg.db.parent)}

    @app.put("/api/sync/arquivo")
    async def api_sync_arquivo(request: Request, caminho: str, tamanho: int, mtime_ns: int, sha256: str):
        exigir_chave(request)
        try:
            alvo = sincronia.destino(caminho, cfg.saidas, cfg.db.parent)
        except sincronia.CaminhoInvalido:
            raise HTTPException(400, "caminho inválido")
        if int(request.headers.get("content-length") or 0) > sincronia.LIMITE_ARQUIVO:
            raise HTTPException(413, "arquivo acima do limite")
        try:
            await sincronia.receber(request.stream(), alvo, tamanho, mtime_ns, sha256)
        except sincronia.CorpoGrandeDemais as exc:
            raise HTTPException(413, str(exc))
        except sincronia.ArquivoRecusado as exc:
            raise HTTPException(422, str(exc))
        return {"ok": True}

    def estado_da_reanalise(idreserva: int) -> dict:
        """O último pedido da reserva e, se ele está na fila, a posição e o
        motivo da espera - para a tela mostrar "aguardando a varredura diária
        terminar" sem precisar saber da trava."""
        pedido = banco.ultimo_pedido(idreserva)
        estado = {"idreserva": idreserva, "pedido": pedido, "posicao": None, "aguardando": None}
        if pedido and pedido["status"] == PEDIDO_NA_FILA:
            estado["posicao"] = banco.posicao_na_fila(pedido["id"])
            estado["aguardando"] = motivo_da_espera()
        return estado

    def estado_da_busca() -> dict:
        """Progresso do último "Buscar reservas novas": fase (na_fila,
        atualizando, analisando, concluida, falhou) e, depois do funil, quantas
        das reservas enfileiradas já terminaram e quantas ficaram de fora pelo
        teto da fila."""
        pedido = banco.ultima_busca_do_funil()
        estado = {"pedido": pedido, "fase": None, "aguardando": None, "total": 0, "analisadas": 0,
                  "falharam": 0, "abertas": 0, "fora_pelo_teto": 0, "sem_analise": None}
        if pedido is None:
            return estado
        if pedido["status"] == PEDIDO_NA_FILA:
            estado.update(fase="na_fila", aguardando=motivo_da_espera())
        elif pedido["status"] == PEDIDO_EM_ANDAMENTO:
            estado["fase"] = "atualizando"
        elif pedido["status"] != PEDIDO_CONCLUIDO:
            estado["fase"] = "falhou"
        else:
            try:
                r = json.loads(pedido["resultado"] or "{}")
            except ValueError:
                r = {}
            seguidas = list(r.get("enfileiradas") or []) + list(r.get("ja_abertas") or [])
            situacoes = banco.status_dos_ultimos_pedidos(seguidas)
            estado.update(
                total=len(seguidas), sem_analise=r.get("sem_analise"),
                fora_pelo_teto=len(r.get("fora_pelo_teto") or []),
                analisadas=sum(1 for s in situacoes.values() if s in (PEDIDO_CONCLUIDO, PEDIDO_FORA_DO_FUNIL)),
                falharam=sum(1 for s in situacoes.values() if s == PEDIDO_FALHOU),
                abertas=sum(1 for i in seguidas if situacoes.get(i) in PEDIDOS_ABERTOS))
            estado["fase"] = "analisando" if estado["abertas"] else "concluida"
        return estado

    @app.post("/api/busca-funil")
    async def api_buscar_reservas_novas(request: Request):
        """ "Buscar reservas novas": um pedido de tipo 'funil' na fila. O executor
        atualiza a listagem do CV e enfileira a análise das reservas do funil
        sem planilha. O site continua sem credenciais."""
        if not csrf_ok(request):
            raise HTTPException(403, "CSRF inválido")
        await ler_corpo(request)
        try:
            _, novo = await run_in_threadpool(banco.pedir_busca_do_funil, "painel", request.state.usuario["login"])
        except BuscaRecente as exc:
            raise HTTPException(429, str(exc))
        return {**await run_in_threadpool(estado_da_busca), "novo": novo}

    @app.get("/api/busca-funil")
    def api_estado_da_busca():
        return estado_da_busca()

    @app.post("/api/reanalise")
    async def api_pedir_reanalise(request: Request):
        """Só grava o pedido na fila: quem chama o CV e a IA é o
        executor_reanalise.py. Reanálise usa o cache da leitura - não existe
        "forçar releitura"."""
        if not csrf_ok(request):
            raise HTTPException(403, "CSRF inválido")
        corpo = await ler_corpo(request)
        try:
            pedido = json.loads(corpo or b"{}")
        except ValueError:
            raise HTTPException(400, "JSON inválido")
        idreserva = pedido.get("idreserva") if isinstance(pedido, dict) else None
        if isinstance(idreserva, bool) or not isinstance(idreserva, int) or idreserva <= 0:
            raise HTTPException(400, "idreserva inválido")
        # A API não aceita o que a tela esconde (pode_reanalisar).
        if idreserva in listagem.ids() and not funil_mod.no_funil(listagem.situacao(idreserva)):
            raise HTTPException(400, "reserva fora do funil segundo a listagem do CV")
        try:
            _, novo = await run_in_threadpool(
                banco.pedir_reanalise, idreserva, "painel", request.state.usuario["login"], listagem.ids())
        except ReservaInexistente:
            raise HTTPException(400, "reserva não encontrada na listagem do CV")
        except (FilaCheia, ReanaliseRecente) as exc:
            raise HTTPException(429, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {**await run_in_threadpool(estado_da_reanalise, idreserva), "novo": novo}

    @app.get("/api/reanalise/{idreserva}")
    def api_estado_da_reanalise(idreserva: int):
        return estado_da_reanalise(idreserva)

    # ------------------------------------------------------------ arquivos
    @app.get("/arquivo/{tipo}/{id_reserva}/{token}")
    def baixar_arquivo(tipo: str, id_reserva: int, token: str):
        """URL opaca: o token é resolvido pelo repositório (só arquivo que a
        planilha conhece), e o caminho ainda passa por todas as defesas de
        caminho_seguro_do_arquivo(). O nome original nunca sai: nem na URL nem
        no Content-Disposition."""
        if tipo not in TIPOS:
            raise HTTPException(404)
        reserva = next((r for r in repo.atualizar() if r.tipo == tipo and r.id == id_reserva), None)
        achado = arquivo_pelo_token(reserva, token) if reserva else None
        if achado is None:
            raise HTTPException(404)
        pasta, arquivo = achado
        alvo = caminho_seguro_do_arquivo(cfg.saidas, pasta.tipo, pasta.nome_pasta, arquivo)
        if alvo is None:
            raise HTTPException(404)
        extensao = alvo.suffix.lower() if RE_EXTENSAO.fullmatch(alvo.suffix.lower()) else ""
        nome_neutro = f"documento{extensao}"
        media = TIPOS_INLINE.get(extensao)
        if media:
            return FileResponse(alvo, media_type=media, filename=nome_neutro, content_disposition_type="inline")
        return FileResponse(alvo, media_type="application/octet-stream", filename=nome_neutro,
                            content_disposition_type="attachment")

    return app
