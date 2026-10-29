"""SQLite do painel: apontamentos, histórico de tratativas, usuários e sessões.

O banco guarda SÓ o que não está nas planilhas - quem tratou o quê, quando e
por quê. O apontamento em si continua vindo de saidas/ a cada leitura; aqui
fica a cópia mínima necessária para o histórico sobreviver à reanálise.

O status de um apontamento é o ÚLTIMO evento dele (sem evento = "aberto"). Log
em vez de coluna de status porque as métricas de impacto precisam do caminho
inteiro: quando foi resolvido, por quem, depois de quanto tempo.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

STATUS_ABERTO = "aberto"
STATUS_CORRIGIDO = "corrigido"
STATUS_IGNORADO = "ignorado"
STATUS_RESOLVIDO_NA_REANALISE = "resolvido_na_reanalise"
STATUS_VALIDOS = (STATUS_ABERTO, STATUS_CORRIGIDO, STATUS_IGNORADO, STATUS_RESOLVIDO_NA_REANALISE)
# resolvido_na_reanalise é só do sistema: ninguém declara que a reanálise resolveu.
STATUS_MANUAIS = (STATUS_ABERTO, STATUS_CORRIGIDO, STATUS_IGNORADO)

MOTIVO_FALSO_POSITIVO = "Falso positivo da IA"
MOTIVO_REGRA_NAO_SE_APLICA = "Regra não se aplica a este caso"
MOTIVO_CORRIGIDO_FORA_DO_CV = "Corrigido fora do CV"
MOTIVOS_IGNORAR = (MOTIVO_FALSO_POSITIVO, MOTIVO_REGRA_NAO_SE_APLICA, MOTIVO_CORRIGIDO_FORA_DO_CV)
PREFIXO_MOTIVO_OUTRO = "Outro: "
# Veredito guiado (a pessoa diz se o apontamento é um problema real). Vem junto
# do status e do motivo, que continuam sendo o que métricas e revisão leem.
VEREDITO_REAL = "real"
VEREDITO_FALSO = "falso"
VEREDITO_NAO_SEI = "nao_sei"
VEREDITOS = (VEREDITO_REAL, VEREDITO_FALSO, VEREDITO_NAO_SEI)
TAMANHO_MAXIMO_MOTIVO = 500
# "Por quê" livre da tratativa (13/10/2026): é o que transforma o ignorado em
# pista para corrigir a regra.
TAMANHO_MAXIMO_DETALHE = 500

USUARIO_SISTEMA = "sistema"

# Fila de reanálise (decisão do João, 19/10/2026): o botão "Reanalisar agora"
# só grava um pedido aqui; quem chama o CV e a IA é o executor_reanalise.py, um
# processo separado - o site continua sem credenciais. 'origem' já nasce pronta
# para o webhook do CV, que virá quando o sistema estiver no servidor da TI.
PEDIDO_NA_FILA = "na_fila"
PEDIDO_EM_ANDAMENTO = "em_andamento"
PEDIDO_CONCLUIDO = "concluido"
PEDIDO_FALHOU = "falhou"
PEDIDO_FORA_DO_FUNIL = "fora_do_funil"
PEDIDOS_ABERTOS = (PEDIDO_NA_FILA, PEDIDO_EM_ANDAMENTO)
PEDIDOS_FINAIS = (PEDIDO_CONCLUIDO, PEDIDO_FALHOU, PEDIDO_FORA_DO_FUNIL)
ORIGENS_DE_PEDIDO = ("painel", "webhook")
TETO_DA_FILA = 20
# Pedido "em_andamento" mais velho que isto é de um executor que morreu no meio.
PEDIDO_ORFAO_APOS = timedelta(minutes=20)
ERRO_EXECUTOR_REINICIADO = "executor reiniciado"


# Tipo do pedido (19/10/2026, "Buscar reservas novas"): 'reserva' é a reanálise
# de uma reserva; 'funil' pede ao executor que atualize a listagem do CV na hora
# e enfileire a análise das reservas do funil ainda sem planilha. O pedido de
# funil usa idreserva 0 - o índice único de "um aberto por reserva" vale para
# ele também: no máximo UMA busca aberta. O webhook do CV pode usar os dois.
PEDIDO_TIPO_RESERVA = "reserva"
PEDIDO_TIPO_FUNIL = "funil"
IDRESERVA_DO_FUNIL = 0
# Intervalo mínimo entre duas buscas, contado do pedido anterior, por origem.
INTERVALO_ENTRE_BUSCAS_POR_ORIGEM: dict[str, timedelta | None] = {
    "painel": timedelta(minutes=2),
    "webhook": None,
}


class ReservaInexistente(ValueError):
    """A reserva não está em reservas_cv.json (a listagem do CV)."""


class FilaCheia(ValueError):
    """Já há TETO_DA_FILA pedidos na fila."""


# Intervalo mínimo entre uma reanálise CONCLUÍDA e o próximo pedido da mesma
# reserva, por origem (revisão da Forja, 19/10/2026). Com o cache, reanálise sem
# documento novo nem chama a IA, mas ainda baixa tudo do CV e segura a trava.
# None = sem intervalo: o webhook do CV terá a regra dele quando existir.
INTERVALO_MINIMO_POR_ORIGEM: dict[str, timedelta | None] = {
    "painel": timedelta(minutes=5),
    "webhook": None,
}


class BuscaRecente(ValueError):
    """Houve busca de reservas novas há menos que o intervalo da origem."""

    def __init__(self, pedido_em: datetime, liberado_em: datetime):
        self.pedido_em, self.liberado_em = pedido_em, liberado_em
        super().__init__(f"busca feita às {pedido_em:%H:%M}, tente de novo depois das "
                         f"{liberado_em:%H:%M}")


class ReanaliseRecente(ValueError):
    """A reserva foi reanalisada há menos que o intervalo mínimo da origem."""

    def __init__(self, concluido_em: datetime, liberado_em: datetime):
        self.concluido_em, self.liberado_em = concluido_em, liberado_em
        super().__init__(f"reanalisada às {concluido_em:%H:%M}, tente de novo depois das "
                         f"{liberado_em:%H:%M}")

# Categoria antiga -> nova, quando o rótulo de testar_amostra.CATEGORIAS muda
# mas o achado é o mesmo. 15/10/2026: a regra do comprovante de endereço virou
# "mês M vale até o fim de M+3" (gestora comercial) e o rótulo dos 90 dias mudou. A chave do
# apontamento usa a categoria (painel/dados.py, identidade_do_apontamento()),
# então lá a categoria nova herda a identidade da antiga - a chave não muda e
# eventos e tratativas continuam ligados. Aqui o banco só troca o rótulo
# gravado, para as métricas não contarem a mesma coisa com dois nomes.
CATEGORIAS_RENOMEADAS = {
    "Comprovante de endereço fora do prazo de 90 dias": "Comprovante de endereço vencido",
}

ESQUEMA = """
CREATE TABLE IF NOT EXISTS apontamentos (
    chave TEXT PRIMARY KEY,
    tipo TEXT NOT NULL,
    id_reserva INTEGER NOT NULL,
    pasta TEXT NOT NULL,
    arquivo TEXT NOT NULL,
    nivel TEXT NOT NULL,
    categoria TEXT NOT NULL,
    texto TEXT NOT NULL,
    primeiro_visto TEXT NOT NULL,
    ultimo_visto TEXT NOT NULL,
    presente INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS apontamentos_pasta ON apontamentos (tipo, pasta);

CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chave TEXT NOT NULL REFERENCES apontamentos (chave),
    status TEXT NOT NULL,
    motivo TEXT,
    usuario TEXT NOT NULL,
    em TEXT NOT NULL,
    detalhe TEXT,
    veredito TEXT
);
CREATE INDEX IF NOT EXISTS eventos_chave ON eventos (chave, id);

-- Última versão de cada planilha já sincronizada: é o que permite dizer que
-- um apontamento SUMIU numa reanálise (planilha mais nova sem ele).
CREATE TABLE IF NOT EXISTS pastas_sincronizadas (
    tipo TEXT NOT NULL,
    pasta TEXT NOT NULL,
    mtime REAL NOT NULL,
    PRIMARY KEY (tipo, pasta)
);

CREATE TABLE IF NOT EXISTS usuarios (
    login TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    perfil TEXT NOT NULL,
    hash TEXT NOT NULL,
    sal TEXT NOT NULL,
    iteracoes INTEGER NOT NULL,
    ativo INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT NOT NULL
);

-- 'token' guarda o SHA-256 do token do cookie, não o token: quem ler o
-- arquivo do banco não consegue se passar por ninguém.
-- Quem criou ou alterou cada usuário (tela de admin, 20/10/2026). Só cresce.
CREATE TABLE IF NOT EXISTS usuarios_eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL,
    acao TEXT NOT NULL,
    detalhe TEXT,
    por TEXT NOT NULL,
    em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessoes (
    token TEXT PRIMARY KEY,
    login TEXT NOT NULL REFERENCES usuarios (login),
    csrf TEXT NOT NULL,
    criado_em TEXT NOT NULL,
    expira_em TEXT NOT NULL
);

-- Valores internos do painel. Hoje só 'segredo': chave aleatória gerada na 1ª
-- execução, usada nos tokens das URLs de arquivo e do cookie pré-login.
CREATE TABLE IF NOT EXISTS configuracao (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

-- Falhas de login, por login e por IP, para o limite de tentativas.
CREATE TABLE IF NOT EXISTS tentativas_login (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL,
    ip TEXT NOT NULL,
    em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tentativas_login_em ON tentativas_login (em);

-- Fila de reanálise (19/10/2026). Escrita SÓ dentro de BEGIN IMMEDIATE
-- (_transacao_imediata): o painel e o executor são processos diferentes.
-- 'origem': 'painel' hoje, 'webhook' no futuro. 'status': na_fila ->
-- em_andamento -> concluido | falhou | fora_do_funil.
-- Opção B (20/10/2026): o executor roda no PC e manda, pela API, o modo da trava
-- de lá. Uma linha só; visto_em velho = "executor desligado".
CREATE TABLE IF NOT EXISTS executor_estado (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    visto_em TEXT NOT NULL,
    modo TEXT
);
CREATE TABLE IF NOT EXISTS pedidos_reanalise (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idreserva INTEGER NOT NULL,
    origem TEXT NOT NULL,
    pedido_por TEXT NOT NULL,
    pedido_em TEXT NOT NULL,
    status TEXT NOT NULL,
    iniciado_em TEXT,
    concluido_em TEXT,
    resultado TEXT,
    erro TEXT,
    tipo TEXT NOT NULL DEFAULT 'reserva'
);
CREATE INDEX IF NOT EXISTS pedidos_reanalise_reserva ON pedidos_reanalise (idreserva, id);
CREATE INDEX IF NOT EXISTS pedidos_reanalise_status ON pedidos_reanalise (status, id);
-- No máximo UM pedido aberto por reserva, garantido pelo próprio banco (além da
-- checagem em pedir_reanalise()).
CREATE UNIQUE INDEX IF NOT EXISTS pedidos_reanalise_um_aberto
    ON pedidos_reanalise (idreserva) WHERE status IN ('na_fila', 'em_andamento');
"""


def agora() -> datetime:
    return datetime.now().replace(microsecond=0)


def para_texto(momento: datetime) -> str:
    return momento.replace(microsecond=0).isoformat(sep=" ")


def de_texto(texto: str | None) -> datetime | None:
    return datetime.fromisoformat(texto) if texto else None


def detalhe_valido(detalhe: str | None) -> str | None:
    """O detalhe como fica gravado: sem espaço nas pontas, vazio = None, e
    CPF/telefone já mascarados - o banco não guarda o número cru que alguém
    colou no "por quê". O limite de TAMANHO_MAXIMO_DETALHE caracteres vale
    para o que a pessoa digitou (é o maxlength do formulário); mais que isso:
    ValueError."""
    if detalhe is None:
        return None
    detalhe = detalhe.strip()
    if len(detalhe) > TAMANHO_MAXIMO_DETALHE:
        raise ValueError(f"detalhe com mais de {TAMANHO_MAXIMO_DETALHE} caracteres")
    # Import tardio: dados importa este módulo.
    from .dados import mascarar_texto_livre
    return mascarar_texto_livre(detalhe) or None


def motivo_valido(motivo: str | None) -> bool:
    """Motivo de 'ignorado': um dos fixos, ou "Outro: <texto>" com texto."""
    if not motivo:
        return False
    if motivo in MOTIVOS_IGNORAR:
        return True
    return (motivo.startswith(PREFIXO_MOTIVO_OUTRO)
            and bool(motivo[len(PREFIXO_MOTIVO_OUTRO):].strip())
            and len(motivo) <= TAMANHO_MAXIMO_MOTIVO)


def _conferir_veredito(veredito: str | None, status: str, motivo: str | None,
                       detalhe: str | None) -> None:
    """Combinações aceitas: real = corrigido, ou ignorado por "Corrigido fora do
    CV"; falso = ignorado por falso positivo, regra que não vale ou "Outro: ...",
    com detalhe; nao_sei = continua aberto, com detalhe. Sem veredito, nada a
    conferir (Reabrir e chamadores antigos)."""
    if veredito is None:
        return
    if veredito not in VEREDITOS:
        raise ValueError(f"veredito inválido: {veredito!r}")
    if veredito == VEREDITO_REAL:
        if status == STATUS_CORRIGIDO or (status == STATUS_IGNORADO and motivo == MOTIVO_CORRIGIDO_FORA_DO_CV):
            return
        raise ValueError('problema real exige status corrigido, ou ignorado com o motivo "Corrigido fora do CV"')
    if veredito == VEREDITO_FALSO:
        if status != STATUS_IGNORADO or motivo == MOTIVO_CORRIGIDO_FORA_DO_CV:
            raise ValueError("aviso errado exige status ignorado com um motivo de aviso errado")
        if not detalhe:
            raise ValueError("aviso errado exige uma explicação")
        return
    if status != STATUS_ABERTO:
        raise ValueError("dúvida mantém o apontamento em aberto")
    if not detalhe:
        raise ValueError("dúvida exige uma explicação")


class Banco:
    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        # Uma escrita por vez: o painel é de poucos usuários, e a sincronização
        # precisa ler o status atual e gravar o evento sem ninguém no meio.
        self._trava = threading.RLock()
        with self.conexao() as con:
            con.executescript(ESQUEMA)
        # Painel e executor sobem juntos no mesmo banco: o "confere e acrescenta
        # a coluna" vai numa transação BEGIN IMMEDIATE (2ª revisão da C2).
        with self._transacao_imediata() as con:
            self._migrar(con)
            con.execute("INSERT OR IGNORE INTO configuracao (chave, valor) VALUES ('segredo', ?)",
                        (secrets.token_hex(32),))
            self._segredo = con.execute(
                "SELECT valor FROM configuracao WHERE chave = 'segredo'").fetchone()["valor"]
        self.migrar_categorias_renomeadas()

    @staticmethod
    def _migrar(con) -> None:
        """Colunas que entraram depois do banco já existir. Só acrescenta:
        o banco real tem as tratativas do João e não pode perder nada."""
        Banco._acrescentar_coluna(con, "eventos", "detalhe", "TEXT")
        # 27/10/2026: veredito guiado ("real", "falso", "nao_sei"); nulo nos eventos antigos.
        Banco._acrescentar_coluna(con, "eventos", "veredito", "TEXT")
        # 19/10/2026: o banco do João já tinha a fila sem a coluna 'tipo'.
        Banco._acrescentar_coluna(con, "pedidos_reanalise", "tipo", "TEXT NOT NULL DEFAULT 'reserva'")
        # 20/10/2026, tela de admin: senha definida pelo admin obriga a troca no
        # primeiro login; último acesso e quem criou/alterou aparecem na lista.
        Banco._acrescentar_coluna(con, "usuarios", "trocar_senha", "INTEGER NOT NULL DEFAULT 0")
        Banco._acrescentar_coluna(con, "usuarios", "ultimo_acesso", "TEXT")
        Banco._acrescentar_coluna(con, "usuarios", "criado_por", "TEXT")
        Banco._acrescentar_coluna(con, "usuarios", "alterado_por", "TEXT")
        Banco._acrescentar_coluna(con, "usuarios", "alterado_em", "TEXT")
        # 21/10/2026: o estado da IA no PC (disjuntor, painel/ia_falhas.py), em JSON.
        Banco._acrescentar_coluna(con, "executor_estado", "ia", "TEXT")

    @staticmethod
    def _acrescentar_coluna(con, tabela: str, coluna: str, definicao: str) -> None:
        """ALTER TABLE ADD COLUMN se a coluna não existe. 'duplicate column'
        = outro processo acabou de acrescentar: já migrado."""
        if coluna in {linha["name"] for linha in con.execute(f"PRAGMA table_info({tabela})")}:
            return
        try:
            con.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def migrar_categorias_renomeadas(self) -> Path | None:
        """Troca o rótulo antigo pelo novo (CATEGORIAS_RENOMEADAS) nos
        apontamentos já gravados. Só a coluna 'categoria' muda: chave, eventos
        e tratativas ficam como estão.

        Idempotente: sem linha com rótulo antigo, não faz nada - nem backup.
        Com linha a migrar, ANTES copia o banco inteiro para
        'painel.db.bak-AAAA-MM-DD_HHMMSS-<microssegundos>-<PID>' (API de backup
        do SQLite). Devolve o caminho do backup, ou None se nada mudou.

        Entre PROCESSOS (2ª revisão da Forja, 15/10/2026): a contagem, o backup
        e o UPDATE acontecem dentro de UM 'BEGIN IMMEDIATE'. Dois painéis
        abrindo juntos ficam em fila no SQLite, e o segundo já conta zero - não
        faz backup nem migra de novo. O nome do backup é criado em modo
        exclusivo: dois backups nunca caem no mesmo arquivo."""
        antigos = list(CATEGORIAS_RENOMEADAS)
        marcas = ",".join("?" * len(antigos))
        with self._trava:
            con = sqlite3.connect(self.caminho, timeout=30, isolation_level=None)
            try:
                con.execute("BEGIN IMMEDIATE")
                pendentes = con.execute(
                    f"SELECT COUNT(*) FROM apontamentos WHERE categoria IN ({marcas})", antigos
                ).fetchone()[0]
                if not pendentes:
                    con.execute("ROLLBACK")
                    return None
                momento = datetime.now()
                backup = self.caminho.with_name(
                    f"{self.caminho.name}.bak-{momento:%Y-%m-%d_%H%M%S}-{momento:%f}-{os.getpid()}")
                os.close(os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                # O backup sai de OUTRA conexão: pela que está no BEGIN
                # IMMEDIATE o SQLite responde "ocupado" para sempre (medido em
                # 15/10/2026 - o processo ficou parado). A trava de escrita
                # desta conexão garante que ninguém muda o banco no meio, e a
                # leitura é fechada antes do COMMIT, que precisa dela solta.
                leitura = sqlite3.connect(self.caminho, timeout=30)
                destino = sqlite3.connect(backup)
                try:
                    leitura.backup(destino)
                finally:
                    destino.close()
                    leitura.close()
                for antiga, nova in CATEGORIAS_RENOMEADAS.items():
                    con.execute("UPDATE apontamentos SET categoria = ? WHERE categoria = ?", (nova, antiga))
                con.execute("COMMIT")
                return backup
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    @property
    def segredo(self) -> bytes:
        return bytes.fromhex(self._segredo)

    @contextmanager
    def conexao(self):
        with self._trava:
            con = sqlite3.connect(self.caminho, timeout=10)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            try:
                with con:
                    yield con
            finally:
                con.close()

    # ------------------------------------------------------------ apontamentos
    @staticmethod
    def _status_atual(con, chave: str) -> str:
        linha = con.execute(
            "SELECT status FROM eventos WHERE chave = ? ORDER BY id DESC LIMIT 1", (chave,)
        ).fetchone()
        return linha["status"] if linha else STATUS_ABERTO

    def sincronizar_pasta(
        self, tipo: str, pasta: str, id_reserva: int, mtime: float, apontamentos: list[dict],
    ) -> bool:
        """Registra os apontamentos da planilha de UMA pasta. Devolve False se
        esta versão da planilha (mtime) já tinha sido sincronizada.

        'apontamentos': dicts com chave, arquivo, nivel, categoria, texto.
        Quem sumiu numa planilha MAIS NOVA foi resolvido na reanálise (a menos
        que alguém já o tenha tratado); quem reaparece volta a ficar aberto -
        exceto o ignorado, que continua ignorado: a pessoa já disse que aquilo
        não é problema, e a IA repetir o achado não muda isso.
        """
        momento = para_texto(datetime.fromtimestamp(mtime))
        with self.conexao() as con:
            anterior = con.execute(
                "SELECT mtime FROM pastas_sincronizadas WHERE tipo = ? AND pasta = ?", (tipo, pasta)
            ).fetchone()
            if anterior is not None and mtime <= anterior["mtime"]:
                return False

            existentes = {
                linha["chave"]: linha["presente"]
                for linha in con.execute(
                    "SELECT chave, presente FROM apontamentos WHERE tipo = ? AND pasta = ?", (tipo, pasta))
            }
            vistos: set[str] = set()
            for a in apontamentos:
                chave = a["chave"]
                vistos.add(chave)
                if chave not in existentes:
                    con.execute(
                        "INSERT INTO apontamentos (chave, tipo, id_reserva, pasta, arquivo, nivel, "
                        "categoria, texto, primeiro_visto, ultimo_visto, presente) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                        (chave, tipo, id_reserva, pasta, a["arquivo"], a["nivel"], a["categoria"],
                         a["texto"], momento, momento))
                    continue
                con.execute(
                    "UPDATE apontamentos SET texto = ?, categoria = ?, ultimo_visto = ?, presente = 1 "
                    "WHERE chave = ?", (a["texto"], a["categoria"], momento, chave))
                if existentes[chave] == 0:
                    if self._status_atual(con, chave) in (STATUS_RESOLVIDO_NA_REANALISE, STATUS_CORRIGIDO):
                        con.execute(
                            "INSERT INTO eventos (chave, status, motivo, usuario, em) VALUES (?, ?, ?, ?, ?)",
                            (chave, STATUS_ABERTO, "Reapareceu na reanálise", USUARIO_SISTEMA, momento))

            for chave, presente in existentes.items():
                if chave in vistos or not presente:
                    continue
                con.execute("UPDATE apontamentos SET presente = 0 WHERE chave = ?", (chave,))
                if self._status_atual(con, chave) not in (STATUS_CORRIGIDO, STATUS_IGNORADO):
                    con.execute(
                        "INSERT INTO eventos (chave, status, motivo, usuario, em) VALUES (?, ?, ?, ?, ?)",
                        (chave, STATUS_RESOLVIDO_NA_REANALISE, None, USUARIO_SISTEMA, momento))

            con.execute(
                "INSERT INTO pastas_sincronizadas (tipo, pasta, mtime) VALUES (?, ?, ?) "
                "ON CONFLICT (tipo, pasta) DO UPDATE SET mtime = excluded.mtime",
                (tipo, pasta, mtime))
        return True

    def status_atuais(self) -> dict[str, dict]:
        """{chave: último evento} - só de quem tem evento."""
        with self.conexao() as con:
            linhas = con.execute(
                "SELECT e.chave, e.status, e.motivo, e.detalhe, e.veredito, e.usuario, e.em FROM eventos e "
                "JOIN (SELECT chave, MAX(id) AS ultimo FROM eventos GROUP BY chave) u "
                "ON e.id = u.ultimo").fetchall()
        return {linha["chave"]: dict(linha) for linha in linhas}

    def listar_apontamentos(self, tipo: str | None = None) -> list[dict]:
        """'tipo': só os apontamentos desse tipo de pasta (o painel passa
        'reserva' desde 20/10/2026; os de pré-cadastro ficam no banco)."""
        with self.conexao() as con:
            if tipo is None:
                return [dict(linha) for linha in con.execute("SELECT * FROM apontamentos")]
            return [dict(linha) for linha in con.execute("SELECT * FROM apontamentos WHERE tipo = ?", (tipo,))]

    def listar_eventos(self, tipo: str | None = None) -> list[dict]:
        """Em ordem de id. 'tipo': só os eventos de apontamentos desse tipo."""
        with self.conexao() as con:
            if tipo is None:
                return [dict(linha) for linha in con.execute("SELECT * FROM eventos ORDER BY id")]
            return [dict(linha) for linha in con.execute(
                "SELECT e.* FROM eventos e JOIN apontamentos a ON a.chave = e.chave "
                "WHERE a.tipo = ? ORDER BY e.id", (tipo,))]

    def buscar_apontamento(self, chave: str) -> dict | None:
        with self.conexao() as con:
            linha = con.execute("SELECT * FROM apontamentos WHERE chave = ?", (chave,)).fetchone()
        return dict(linha) if linha else None

    def registrar_tratativa(self, chave: str, status: str, motivo: str | None, usuario: str,
                            detalhe: str | None = None, veredito: str | None = None) -> dict:
        """Grava a decisão de uma pessoa. A validação de status, motivo e
        detalhe é feita aqui, e não só na rota, para nenhum chamador conseguir
        pular. O detalhe vale para qualquer status (o uso é no ignorado).
        'veredito' (opcional): ver _conferir_veredito; sem ele vale o de sempre."""
        detalhe = detalhe_valido(detalhe)
        if status not in STATUS_MANUAIS:
            raise ValueError(f"status inválido: {status!r}")
        if status == STATUS_IGNORADO:
            if not motivo_valido(motivo):
                raise ValueError("ignorar exige um motivo válido")
        else:
            motivo = None
        _conferir_veredito(veredito, status, motivo, detalhe)
        with self.conexao() as con:
            if con.execute("SELECT 1 FROM apontamentos WHERE chave = ?", (chave,)).fetchone() is None:
                raise KeyError(chave)
            em = para_texto(agora())
            con.execute(
                "INSERT INTO eventos (chave, status, motivo, usuario, em, detalhe, veredito) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chave, status, motivo, usuario, em, detalhe, veredito))
        return {"chave": chave, "status": status, "motivo": motivo, "detalhe": detalhe,
                "veredito": veredito, "usuario": usuario, "em": em}

    # ------------------------------------------------------------ fila de reanálise
    @contextmanager
    def _transacao_imediata(self):
        """Conexão em BEGIN IMMEDIATE ... COMMIT (ROLLBACK se der erro), para as
        escritas da fila. O painel e o executor são PROCESSOS diferentes, e
        "confere e grava" (duplicidade, teto, pegar o próximo da fila) precisa
        da trava de escrita do SQLite já na leitura: a transação adiada do
        'with con' de conexao() só a pede no primeiro INSERT, e dois processos
        poderiam conferir juntos e gravar os dois."""
        with self._trava:
            con = sqlite3.connect(self.caminho, timeout=30, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()

    def pedir_reanalise(self, idreserva: int, origem: str, pedido_por: str,
                        reservas_validas) -> tuple[dict, bool]:
        """Põe uma reserva na fila de reanálise. Devolve (pedido, novo).

        As regras são conferidas AQUI, e não só na rota, para nenhum chamador -
        o painel hoje, o webhook do CV amanhã - conseguir pular:
          - a reserva tem de estar em 'reservas_validas' (os números de
            reservas_cv.json) -> ReservaInexistente;
          - já existe pedido aberto (na fila ou em andamento) para ela -> devolve
            ESSE pedido, com novo=False, em vez de criar outro;
          - foi concluída há menos que INTERVALO_MINIMO_POR_ORIGEM[origem] ->
            ReanaliseRecente;
          - a fila já tem TETO_DA_FILA pedidos -> FilaCheia.
        """
        if isinstance(idreserva, bool) or not isinstance(idreserva, int) or idreserva <= 0:
            raise ValueError("idreserva inválido")
        if origem not in ORIGENS_DE_PEDIDO:
            raise ValueError(f"origem inválida: {origem!r}")
        if not isinstance(pedido_por, str) or not pedido_por.strip():
            raise ValueError("pedido sem autor")
        if idreserva not in reservas_validas:
            raise ReservaInexistente(idreserva)
        with self._transacao_imediata() as con:
            aberto = con.execute(
                "SELECT * FROM pedidos_reanalise WHERE idreserva = ? AND status IN (?, ?) "
                "ORDER BY id DESC LIMIT 1", (idreserva, *PEDIDOS_ABERTOS)).fetchone()
            if aberto is not None:
                return dict(aberto), False
            intervalo = INTERVALO_MINIMO_POR_ORIGEM.get(origem)
            if intervalo:
                ultimo = con.execute(
                    "SELECT concluido_em FROM pedidos_reanalise WHERE idreserva = ? AND status = ? "
                    "ORDER BY concluido_em DESC LIMIT 1", (idreserva, PEDIDO_CONCLUIDO)).fetchone()
                concluido = de_texto(ultimo["concluido_em"]) if ultimo else None
                if concluido and agora() < concluido + intervalo:
                    raise ReanaliseRecente(concluido, concluido + intervalo)
            na_fila = con.execute(
                "SELECT COUNT(*) FROM pedidos_reanalise WHERE status = ? AND tipo = ?",
                (PEDIDO_NA_FILA, PEDIDO_TIPO_RESERVA)).fetchone()[0]
            if na_fila >= TETO_DA_FILA:
                raise FilaCheia(f"a fila já tem {na_fila} pedidos (máximo {TETO_DA_FILA})")
            cursor = con.execute(
                "INSERT INTO pedidos_reanalise (idreserva, origem, pedido_por, pedido_em, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (idreserva, origem, pedido_por.strip(), para_texto(agora()), PEDIDO_NA_FILA))
            novo = con.execute("SELECT * FROM pedidos_reanalise WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(novo), True

    def enfileirar_varias(self, ids: list[int], origem: str, pedido_por: str, reservas_validas) -> dict:
        """pedir_reanalise() de cada id, na ordem ("Buscar reservas novas": as
        mais novas primeiro, para o teto cortar as mais velhas). Devolve as listas
        enfileiradas / ja_abertas / fora_pelo_teto / recusadas."""
        saida = {"enfileiradas": [], "ja_abertas": [], "fora_pelo_teto": [], "recusadas": []}
        for idreserva in ids:
            try:
                _, novo = self.pedir_reanalise(idreserva, origem, pedido_por, reservas_validas)
            except FilaCheia:
                saida["fora_pelo_teto"].append(idreserva)
                continue
            except ValueError:  # reanalisada há menos de 5 min, ou saiu da listagem
                saida["recusadas"].append(idreserva)
                continue
            saida["enfileiradas" if novo else "ja_abertas"].append(idreserva)
        return saida

    def buscar_pedido(self, id_pedido: int) -> dict | None:
        with self.conexao() as con:
            linha = con.execute("SELECT * FROM pedidos_reanalise WHERE id = ?", (id_pedido,)).fetchone()
        return dict(linha) if linha else None

    def registrar_estado_do_executor(self, modo: str | None, ia: str | None = None) -> None:
        """'ia': o estado da IA no PC, em JSON ("{}" = o PC não tem disjuntor
        armado nem registro); None = executor antigo, que não manda."""
        with self.conexao() as con:
            con.execute("INSERT OR REPLACE INTO executor_estado (id, visto_em, modo, ia) VALUES (1, ?, ?, ?)",
                        (para_texto(agora()), modo, ia))

    def estado_do_executor(self) -> dict | None:
        with self.conexao() as con:
            linha = con.execute("SELECT visto_em, modo, ia FROM executor_estado WHERE id = 1").fetchone()
        return dict(linha) if linha else None

    def ultimo_pedido(self, idreserva: int) -> dict | None:
        with self.conexao() as con:
            linha = con.execute(
                "SELECT * FROM pedidos_reanalise WHERE idreserva = ? AND tipo = ? ORDER BY id DESC LIMIT 1",
                (idreserva, PEDIDO_TIPO_RESERVA)).fetchone()
        return dict(linha) if linha else None

    def pedir_busca_do_funil(self, origem: str, pedido_por: str) -> tuple[dict, bool]:
        """"Buscar reservas novas": põe na fila um pedido de tipo 'funil'.
        Devolve (pedido, novo). Busca já aberta -> devolve ESSA. Busca há menos
        que INTERVALO_ENTRE_BUSCAS_POR_ORIGEM[origem] -> BuscaRecente."""
        if origem not in ORIGENS_DE_PEDIDO:
            raise ValueError(f"origem inválida: {origem!r}")
        if not isinstance(pedido_por, str) or not pedido_por.strip():
            raise ValueError("pedido sem autor")
        with self._transacao_imediata() as con:
            ultima = con.execute(
                "SELECT * FROM pedidos_reanalise WHERE tipo = ? ORDER BY id DESC LIMIT 1",
                (PEDIDO_TIPO_FUNIL,)).fetchone()
            if ultima is not None and ultima["status"] in PEDIDOS_ABERTOS:
                return dict(ultima), False
            intervalo = INTERVALO_ENTRE_BUSCAS_POR_ORIGEM.get(origem)
            if intervalo and ultima is not None:
                anterior = de_texto(ultima["pedido_em"])
                if agora() < anterior + intervalo:
                    raise BuscaRecente(anterior, anterior + intervalo)
            cursor = con.execute(
                "INSERT INTO pedidos_reanalise (idreserva, origem, pedido_por, pedido_em, status, tipo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (IDRESERVA_DO_FUNIL, origem, pedido_por.strip(), para_texto(agora()), PEDIDO_NA_FILA,
                 PEDIDO_TIPO_FUNIL))
            novo = con.execute("SELECT * FROM pedidos_reanalise WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(novo), True

    def ultima_busca_do_funil(self) -> dict | None:
        with self.conexao() as con:
            linha = con.execute("SELECT * FROM pedidos_reanalise WHERE tipo = ? ORDER BY id DESC LIMIT 1",
                                (PEDIDO_TIPO_FUNIL,)).fetchone()
        return dict(linha) if linha else None

    def status_dos_ultimos_pedidos(self, idreservas: list[int]) -> dict[int, str]:
        """{idreserva: status do último pedido de reanálise} - para o progresso
        de uma busca."""
        if not idreservas:
            return {}
        marcas = ",".join("?" * len(idreservas))
        with self.conexao() as con:
            linhas = con.execute(
                f"SELECT p.idreserva, p.status FROM pedidos_reanalise p JOIN "
                f"(SELECT idreserva, MAX(id) AS ultimo FROM pedidos_reanalise WHERE tipo = ? "
                f"AND idreserva IN ({marcas}) GROUP BY idreserva) u ON p.id = u.ultimo",
                (PEDIDO_TIPO_RESERVA, *idreservas)).fetchall()
        return {linha["idreserva"]: linha["status"] for linha in linhas}

    def posicao_na_fila(self, id_pedido: int) -> int | None:
        """1 = o próximo a ser atendido. None se o pedido não está na fila."""
        with self.conexao() as con:
            linha = con.execute("SELECT status FROM pedidos_reanalise WHERE id = ?", (id_pedido,)).fetchone()
            if linha is None or linha["status"] != PEDIDO_NA_FILA:
                return None
            return con.execute("SELECT COUNT(*) FROM pedidos_reanalise WHERE status = ? AND id <= ?",
                               (PEDIDO_NA_FILA, id_pedido)).fetchone()[0]

    def ha_pedido_na_fila(self) -> bool:
        with self.conexao() as con:
            return con.execute("SELECT 1 FROM pedidos_reanalise WHERE status = ? LIMIT 1",
                               (PEDIDO_NA_FILA,)).fetchone() is not None

    def pegar_proximo_pedido(self) -> dict | None:
        """Para o executor: o pedido mais antigo da fila passa a em_andamento,
        numa transação só - dois executores nunca pegam o mesmo."""
        with self._transacao_imediata() as con:
            linha = con.execute("SELECT * FROM pedidos_reanalise WHERE status = ? ORDER BY id LIMIT 1",
                                (PEDIDO_NA_FILA,)).fetchone()
            if linha is None:
                return None
            iniciado = para_texto(agora())
            con.execute("UPDATE pedidos_reanalise SET status = ?, iniciado_em = ? WHERE id = ?",
                        (PEDIDO_EM_ANDAMENTO, iniciado, linha["id"]))
        return dict(linha, status=PEDIDO_EM_ANDAMENTO, iniciado_em=iniciado)

    def concluir_pedido(self, id_pedido: int, status: str, resultado: str | None = None,
                        erro: str | None = None) -> bool:
        """Fecha um pedido em andamento. False se ele não estava mais em andamento
        (ex.: virou "falhou" por estar órfão)."""
        if status not in PEDIDOS_FINAIS:
            raise ValueError(f"status final inválido: {status!r}")
        with self._transacao_imediata() as con:
            cursor = con.execute(
                "UPDATE pedidos_reanalise SET status = ?, concluido_em = ?, resultado = ?, erro = ? "
                "WHERE id = ? AND status = ?",
                (status, para_texto(agora()), resultado, erro, id_pedido, PEDIDO_EM_ANDAMENTO))
            return cursor.rowcount == 1

    def recuperar_pedidos_orfaos(self, agora_: datetime | None = None) -> list[int]:
        """Pedido "em_andamento" há mais de PEDIDO_ORFAO_APOS é de um executor que
        morreu no meio: vira "falhou" com ERRO_EXECUTOR_REINICIADO. Devolve os
        ids. O executor chama ao subir (e a cada volta: ele atende um pedido por
        vez, então fora da própria análise nada dele está em andamento)."""
        corte = para_texto((agora_ or agora()) - PEDIDO_ORFAO_APOS)
        with self._transacao_imediata() as con:
            ids = [linha["id"] for linha in con.execute(
                "SELECT id FROM pedidos_reanalise WHERE status = ? AND iniciado_em < ?",
                (PEDIDO_EM_ANDAMENTO, corte))]
            for id_pedido in ids:
                con.execute(
                    "UPDATE pedidos_reanalise SET status = ?, concluido_em = ?, erro = ? WHERE id = ?",
                    (PEDIDO_FALHOU, para_texto(agora_ or agora()), ERRO_EXECUTOR_REINICIADO, id_pedido))
        return ids

    # ------------------------------------------------------------ usuários
    def criar_usuario(self, login: str, nome: str, perfil: str, hash_: str, sal: str, iteracoes: int,
                      por: str = "terminal", trocar_senha: bool = False) -> None:
        """'trocar_senha': a senha foi definida por outra pessoa (o admin) e
        precisa ser trocada no primeiro login. Login repetido: sqlite3.IntegrityError."""
        quando = para_texto(agora())
        with self._transacao_imediata() as con:
            con.execute(
                "INSERT INTO usuarios (login, nome, perfil, hash, sal, iteracoes, ativo, criado_em, "
                "trocar_senha, criado_por) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (login, nome, perfil, hash_, sal, iteracoes, quando, int(trocar_senha), por))
            self._evento_de_usuario(con, login, "criado", f"perfil {perfil}", por, quando)

    def buscar_usuario(self, login: str) -> dict | None:
        with self.conexao() as con:
            linha = con.execute("SELECT * FROM usuarios WHERE login = ?", (login,)).fetchone()
        return dict(linha) if linha else None

    def listar_usuarios(self) -> list[dict]:
        """Sem hash nem sal."""
        with self.conexao() as con:
            return [dict(linha) for linha in con.execute(
                "SELECT login, nome, perfil, ativo, criado_em, criado_por, alterado_por, alterado_em, "
                "ultimo_acesso, trocar_senha FROM usuarios ORDER BY login")]

    def eventos_de_usuarios(self, limite: int = 50) -> list[dict]:
        with self.conexao() as con:
            return [dict(linha) for linha in con.execute(
                "SELECT * FROM usuarios_eventos ORDER BY id DESC LIMIT ?", (limite,))]

    @staticmethod
    def _evento_de_usuario(con, login: str, acao: str, detalhe: str | None, por: str, quando: str) -> None:
        con.execute("INSERT INTO usuarios_eventos (login, acao, detalhe, por, em) VALUES (?, ?, ?, ?, ?)",
                    (login, acao, detalhe, por, quando))

    def _alterar_usuario(self, login: str, campos: dict, acao: str, detalhe: str | None, por: str,
                         derrubar_sessoes: bool, manter_sessao: str | None = None) -> None:
        quando = para_texto(agora())
        atribuicoes = ", ".join(f"{c} = ?" for c in campos)
        with self._transacao_imediata() as con:
            cursor = con.execute(
                f"UPDATE usuarios SET {atribuicoes}, alterado_por = ?, alterado_em = ? WHERE login = ?",
                (*campos.values(), por, quando, login))
            if cursor.rowcount != 1:
                raise KeyError(login)
            if derrubar_sessoes:
                con.execute("DELETE FROM sessoes WHERE login = ? AND token != ?", (login, manter_sessao or ""))
            self._evento_de_usuario(con, login, acao, detalhe, por, quando)

    def definir_ativo(self, login: str, ativo: bool, por: str) -> None:
        """Desativar derruba todas as sessões dele."""
        self._alterar_usuario(login, {"ativo": int(ativo)}, "reativado" if ativo else "desativado", None, por,
                              derrubar_sessoes=not ativo)

    def definir_perfil(self, login: str, perfil: str, por: str) -> None:
        self._alterar_usuario(login, {"perfil": perfil}, "perfil", perfil, por, derrubar_sessoes=False)

    def definir_senha(self, login: str, hash_: str, sal: str, iteracoes: int, por: str, trocar_senha: bool,
                      manter_sessao: str | None = None) -> None:
        """Senha nova. Derruba as sessões dele (menos 'manter_sessao', o hash do
        token de quem está trocando a própria senha)."""
        self._alterar_usuario(
            login, {"hash": hash_, "sal": sal, "iteracoes": iteracoes, "trocar_senha": int(trocar_senha)},
            "senha_redefinida" if trocar_senha else "senha_trocada", None, por,
            derrubar_sessoes=True, manter_sessao=manter_sessao)

    def registrar_acesso(self, login: str) -> None:
        with self.conexao() as con:
            con.execute("UPDATE usuarios SET ultimo_acesso = ? WHERE login = ?", (para_texto(agora()), login))

    # ------------------------------------------------------------ sessões
    def criar_sessao(self, token_hash: str, login: str, csrf: str, criado: datetime, expira: datetime) -> None:
        with self.conexao() as con:
            con.execute("DELETE FROM sessoes WHERE expira_em <= ?", (para_texto(criado),))
            con.execute(
                "INSERT INTO sessoes (token, login, csrf, criado_em, expira_em) VALUES (?, ?, ?, ?, ?)",
                (token_hash, login, csrf, para_texto(criado), para_texto(expira)))

    def buscar_sessao(self, token_hash: str) -> dict | None:
        with self.conexao() as con:
            linha = con.execute(
                "SELECT s.csrf, s.expira_em, u.login, u.nome, u.perfil, u.ativo, u.trocar_senha FROM sessoes s "
                "JOIN usuarios u ON u.login = s.login WHERE s.token = ?", (token_hash,)).fetchone()
        return dict(linha) if linha else None

    def apagar_sessao(self, token_hash: str) -> None:
        with self.conexao() as con:
            con.execute("DELETE FROM sessoes WHERE token = ?", (token_hash,))

    # ------------------------------------------------------------ tentativas de login
    def falhas_recentes(self, login: str, ip: str, desde: datetime) -> tuple[int, int]:
        """(falhas deste login, falhas deste IP) desde 'desde'."""
        corte = para_texto(desde)
        with self.conexao() as con:
            por_login = con.execute(
                "SELECT COUNT(*) FROM tentativas_login WHERE login = ? AND em > ?", (login, corte)).fetchone()[0]
            por_ip = con.execute(
                "SELECT COUNT(*) FROM tentativas_login WHERE ip = ? AND em > ?", (ip, corte)).fetchone()[0]
        return por_login, por_ip

    def registrar_falha(self, login: str, ip: str, quando: datetime) -> None:
        with self.conexao() as con:
            con.execute("DELETE FROM tentativas_login WHERE em < ?", (para_texto(quando - timedelta(days=1)),))
            con.execute("INSERT INTO tentativas_login (login, ip, em) VALUES (?, ?, ?)",
                        (login, ip, para_texto(quando)))

    def zerar_falhas_do_login(self, login: str) -> None:
        with self.conexao() as con:
            con.execute("DELETE FROM tentativas_login WHERE login = ?", (login,))
