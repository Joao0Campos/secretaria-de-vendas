"""
Envio do PC para o servidor, pelo próprio site (opção B, 20/10/2026)
====================================================================

Mão única: o PC manda saidas/reserva/<pasta>/<arquivo> e, de painel_dados/,
reservas_cv.json, varreduras.jsonl e ultima_varredura_ok.txt. O painel.db fica
SÓ no servidor e só o site grava nele.

COMO: monta o manifesto (caminho relativo, tamanho, mtime em ns), pergunta ao
site (POST /api/sync/manifesto) o que falta ou mudou e envia só isso, um arquivo
por pedido (PUT /api/sync/arquivo), com o sha256 calculado na hora. O site grava
de forma atômica, confere o sha256 e preserva o mtime.

- Rodada rápida (a de 5 min): compara só tamanho e mtime.
- Conferência completa: o manifesto leva o sha256 de TODOS os arquivos (sem
  cache) e o site compara com o dele - pega o arquivo corrigido que manteve
  tamanho e mtime. Uma vez por dia (a primeira rodada depois das 3h) e com
  --completo.
- Apagar no servidor (só em saidas/reserva) o que sumiu do PC: só quando o
  manifesto está COMPLETO. Qualquer falha ao enumerar ou ler um arquivo local
  marca o manifesto como incompleto, e naquela rodada nada é apagado (no log). As planilhas e os documentos vão antes dos
arquivos de painel_dados/: o site nunca mostra a listagem nova com a planilha
velha. A carga inicial (~870 MB) é a mesma rotina: cada arquivo enviado fica,
e a próxima execução continua de onde parou.

QUANDO: no fim de cada varredura, funil e reanálise (o executor espera o envio
antes de dar o pedido como concluído) e pela tarefa a cada 5 min (rede de
segurança). Uma execução por vez (painel_dados/sync.trava). Com
SECRETARIA_MODO_ENVIO local ou sem as variáveis do servidor (PC como hoje)
não faz nada. Falha (rede, servidor fora, arquivo recusado) NUNCA derruba quem
chamou: vai para painel_dados/logs/sync-AAAA-MM-DD.txt e a próxima tenta de novo.

BACKUP FORA DO SERVIDOR: o painel.db existe só no servidor, que faz um backup
por dia às 2h. Uma vez por dia (a primeira rodada depois das 3h), esta rotina
baixa o mais recente (GET /api/executor/backup) para
painel_dados/backups_servidor/, confere o sha256 e a integridade e guarda 14.
Falha só vai para o log.

Como rodar:
    python sincronizar_servidor.py              -> uma sincronização (a tarefa de 5 min)
    python sincronizar_servidor.py --completo   -> conferência completa por sha256
    python sincronizar_servidor.py --comparar   -> SÓ compara PC x servidor (contagem e sha256) e
                                                   mostra as diferenças; não envia nem apaga nada
    python sincronizar_servidor.py --ensaio-falhar-em TEXTO
                                                -> ensaio E7: finge a falha do envio dos arquivos
                                                   cujo caminho contém TEXTO (nada some, a listagem
                                                   não é publicada)
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import cliente_servidor  # noqa: E402

PASTA_SAIDAS = RAIZ / "saidas"
PASTA_DADOS = RAIZ / "painel_dados"
ARQUIVOS_DE_DADOS = ("reservas_cv.json", "varreduras.jsonl", "ultima_varredura_ok.txt", "ia_estado.json")
RE_DADOS_POR_PADRAO = re.compile(r"consumo_ia-\d{4}-\d{2}\.jsonl")  # o consumo da IA, um arquivo por mês
# Obrigatórios: sem eles o site não tem o que mostrar (a listagem do funil e a
# última varredura do topo). Ausente = manifesto incompleto: sem concluir e sem
# exclusões. ultima_varredura_ok.txt é só o marcador do dia no PC (o site não o
# lê): sobe quando existe; consumo_ia-AAAA-MM.jsonl (a tela "Consumo da IA") e
# ia_estado.json (o disjuntor da IA, banner do admin) também são opcionais.
# saidas/reserva/ também é obrigatória (ver _arquivos_locais).
ARQUIVOS_OBRIGATORIOS = ("reservas_cv.json", "varreduras.jsonl")
TRAVA_VELHA_APOS_S = 10 * 60   # a trava é "tocada" a cada arquivo enviado
ESPERA_DA_TRAVA_S = 1.0
HORA_DA_CONFERENCIA = 3        # a primeira rodada depois das 3h confere tudo por sha256
HORA_DO_BACKUP_DO_SERVIDOR = 3  # o servidor faz o dele às 2h
GUARDAR_BACKUPS = 14


def _log(texto: str) -> None:
    try:
        logs = PASTA_DADOS / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / f"sync-{datetime.now():%Y-%m-%d}.txt", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {texto}\n")
    except OSError:
        pass


# ------------------------------------------------------------------ manifesto
def _listar(pasta: Path) -> list[os.DirEntry]:
    """os.scandir explícito: no Python 3.14, is_dir()/is_file() podem engolir um
    erro de acesso e devolver False, e a pasta sumiria do manifesto em silêncio."""
    with os.scandir(pasta) as entradas:
        return sorted(entradas, key=lambda e: e.name)


def _estat(entrada: os.DirEntry) -> os.stat_result:
    return entrada.stat(follow_symlinks=False)


FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _e_link(st) -> bool:
    """Link simbólico, junção ou qualquer reparse point (OneDrive, montagem):
    o conteúdo pode ser de outro lugar, ou sumir. Não entra no manifesto, e o
    manifesto fica incompleto."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _arquivos_locais() -> tuple[list[tuple[str, Path, os.stat_result]], list[str]]:
    """(itens, falhas). Qualquer erro ao listar ou ao ler os metadados, e
    qualquer link ou reparse point, vai para 'falhas' com o caminho (pasta
    termina em '/'): o manifesto fica incompleto e nada é apagado no servidor.
    saidas/reserva ausente também é falha - nunca vira "o PC não tem nada"."""
    itens, falhas = [], []
    raiz = PASTA_SAIDAS / "reserva"
    try:
        if _e_link(os.lstat(raiz)):
            return [], ["saidas/reserva/ (link ou reparse point)"]
        pastas = _listar(raiz)
    except OSError as exc:
        return [], [f"saidas/reserva/ ({type(exc).__name__})"]
    for pasta in pastas:
        prefixo = f"saidas/reserva/{pasta.name}/"
        try:
            st = _estat(pasta)
            if _e_link(st):
                falhas.append(f"{prefixo} (link ou reparse point)")
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            arquivos = _listar(Path(pasta.path))
        except OSError as exc:
            falhas.append(f"{prefixo} ({type(exc).__name__})")
            continue
        for arquivo in arquivos:
            if arquivo.name.startswith("~$"):  # "~$x.xlsx" = planilha aberta no Excel
                continue
            try:
                st = _estat(arquivo)
            except OSError as exc:
                falhas.append(f"{prefixo}{arquivo.name} ({type(exc).__name__})")
                continue
            if _e_link(st):
                falhas.append(f"{prefixo}{arquivo.name} (link ou reparse point)")
            elif stat.S_ISREG(st.st_mode):
                itens.append((prefixo + arquivo.name, Path(arquivo.path), st))
    try:
        por_padrao = sorted(e.name for e in _listar(PASTA_DADOS) if RE_DADOS_POR_PADRAO.fullmatch(e.name))
    except FileNotFoundError:
        por_padrao = []
    except OSError as exc:
        por_padrao = []
        falhas.append(f"painel_dados/ ({type(exc).__name__})")
    for nome in list(ARQUIVOS_DE_DADOS) + por_padrao:  # por último: a listagem só é publicada depois das planilhas
        local = PASTA_DADOS / nome
        try:
            st = os.lstat(local)
        except FileNotFoundError:
            if nome in ARQUIVOS_OBRIGATORIOS:
                falhas.append(f"painel_dados/{nome} (obrigatório ausente)")
            continue
        except OSError as exc:
            falhas.append(f"painel_dados/{nome} ({type(exc).__name__})")
            continue
        if _e_link(st):
            falhas.append(f"painel_dados/{nome} (link ou reparse point)")
        elif stat.S_ISREG(st.st_mode):
            itens.append((f"painel_dados/{nome}", local, st))
    return itens, falhas


def _sha256(caminho: Path) -> str:
    resumo = hashlib.sha256()
    with open(caminho, "rb") as f:
        for pedaco in iter(lambda: f.read(1 << 20), b""):
            resumo.update(pedaco)
    return resumo.hexdigest()


def manifesto(com_sha: bool = False) -> tuple[list[dict], list[str]]:
    """(itens, falhas). Rodada rápida: tamanho e mtime. Conferência completa
    (com_sha): também o sha256 de cada arquivo, calculado agora, sem cache.
    Qualquer falha local entra em 'falhas': manifesto incompleto."""
    locais, falhas = _arquivos_locais()
    itens = []
    for caminho, local, st in locais:
        item = {"caminho": caminho, "tamanho": st.st_size, "mtime_ns": st.st_mtime_ns}
        if com_sha:
            try:
                item["sha256"] = _sha256(local)
            except OSError as exc:
                falhas.append(f"{caminho} ({type(exc).__name__})")
                continue
        itens.append(item)
    return itens, falhas

def conferencia_do_dia_pendente(agora: datetime | None = None) -> bool:
    """A primeira rodada depois das HORA_DA_CONFERENCIA de cada dia confere tudo por sha256."""
    agora = agora or datetime.now()
    if agora.hour < HORA_DA_CONFERENCIA:
        return False
    try:
        ultima = (PASTA_DADOS / "sync_conferencia.txt").read_text(encoding="utf-8").strip()
    except OSError:
        ultima = ""
    return ultima != agora.strftime("%Y-%m-%d")


# ------------------------------------------------------------------ trava
def _pegar_trava(esperar_s: float) -> Path | None:
    trava = PASTA_DADOS / "sync.trava"
    limite = time.monotonic() + esperar_s
    while True:
        try:
            PASTA_DADOS.mkdir(parents=True, exist_ok=True)
            fd = os.open(trava, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return trava
        except FileExistsError:
            try:
                if time.time() - trava.stat().st_mtime > TRAVA_VELHA_APOS_S:  # dono morreu no meio
                    trava.unlink(missing_ok=True)
                    continue
            except OSError:
                continue
        except OSError:
            return None
        if time.monotonic() >= limite:
            return None
        time.sleep(ESPERA_DA_TRAVA_S)


# ------------------------------------------------------------------ backup do servidor
def backup_do_dia_pendente(agora: datetime | None = None) -> bool:
    agora = agora or datetime.now()
    if agora.hour < HORA_DO_BACKUP_DO_SERVIDOR:
        return False
    try:
        ultimo = (PASTA_DADOS / "backups_servidor" / "ultimo.txt").read_text(encoding="utf-8").strip()
    except OSError:
        ultimo = ""
    return ultimo != agora.strftime("%Y-%m-%d")


def baixar_backup(cliente) -> Path | None:
    """Baixa o backup mais recente do servidor, confere sha256 e integridade,
    guarda GUARDAR_BACKUPS. Nunca levanta exceção (log)."""
    from painel import backup as backup_mod  # só biblioteca padrão

    pasta = PASTA_DADOS / "backups_servidor"
    temporario = pasta / f".baixando.{os.getpid()}.parcial"
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        cabecalhos = {k.lower(): v for k, v in cliente.baixar("/api/executor/backup", temporario).items()}
        esperado = (cabecalhos.get("x-backup-sha256") or "").lower()
        nome = re.search(r'filename="?([^";]+)"?', cabecalhos.get("content-disposition") or "")
        nome = nome.group(1) if nome else ""
        if not backup_mod.RE_NOME.fullmatch(nome):
            raise ValueError(f"nome de backup inesperado: {nome[:60]!r}")
        if not esperado or backup_mod.sha256(temporario) != esperado:
            raise ValueError("sha256 não confere com o do servidor")
        resultado = backup_mod.integridade(temporario)
        if resultado != "ok":
            raise ValueError(f"integrity_check: {resultado[:100]}")
        destino = pasta / nome
        os.replace(temporario, destino)
        apagados = backup_mod.rotacionar(pasta, GUARDAR_BACKUPS)
        (pasta / "ultimo.txt").write_text(datetime.now().strftime("%Y-%m-%d"), encoding="utf-8")
        _log(f"backup do servidor: {nome} baixado ({destino.stat().st_size / 2**20:.1f} MB, sha256 e "
             f"integrity_check ok); {len(apagados)} antigo(s) apagado(s)")
        return destino
    except cliente_servidor.ServidorIndisponivel as exc:
        _log(f"backup do servidor: não baixado - {exc}")
    except Exception as exc:  # nunca derruba o envio
        _log(f"backup do servidor: FALHOU - {type(exc).__name__}: {exc}")
    finally:
        temporario.unlink(missing_ok=True)
    return None


# ------------------------------------------------------------------ sincronizar
@dataclass
class Resultado:
    """O que uma sincronização fez. 'nao_entregues': caminhos (ou pastas, com
    '/' no fim) que deviam estar no servidor e não ficaram nesta rodada."""

    ok: bool
    local: bool = False
    erro: str | None = None
    nao_entregues: list[str] = field(default_factory=list)

    def reserva_entregue(self, idreserva: int) -> bool:
        """Todas as pastas da reserva (titular e associados) chegaram ao servidor?"""
        if self.local:
            return True
        if self.erro:
            return False
        marca = f"-reserva-{idreserva}/"
        return not any(marca in c or c == "saidas/reserva/" for c in self.nao_entregues)


def sincronizar_detalhado(motivo: str, esperar_s: float = 0, cliente=None,
                          completo: bool | None = None, falhar_em: str | None = None) -> Resultado:
    """Nunca levanta exceção. 'completo': conferência por sha256 (None = só se
    for a do dia). Publicação por último: reservas_cv.json, varreduras.jsonl e
    ultima_varredura_ok.txt só sobem se TODAS as planilhas e documentos da
    rodada subiram e o manifesto está completo - o site nunca mostra a listagem
    nova com uma planilha que não chegou. Exclusões no servidor: só no fim
    (/api/sync/concluir), depois de todos os envios e da publicação, e só com
    o manifesto completo. 'falhar_em' (só o ensaio E7, pela linha de comando):
    finge a falha do envio de todo arquivo cujo caminho contém o texto."""
    if cliente is None:
        try:
            if cliente_servidor.modo("envio") == "local":
                return Resultado(ok=True, local=True)
        except cliente_servidor.ConfiguracaoInvalida as exc:
            _log(f"{motivo}: configuração do servidor inválida - nada enviado: {exc}")
            return Resultado(ok=False, erro=str(exc))
    trava = _pegar_trava(esperar_s)
    if trava is None:
        _log(f"{motivo}: outra sincronização em andamento - fica para ela")
        return Resultado(ok=False, erro="outra sincronização em andamento")
    comeco = time.monotonic()
    try:
        cliente = cliente or cliente_servidor.Cliente()
        conferir = conferencia_do_dia_pendente() if completo is None else completo
        itens, falhas_locais = manifesto(com_sha=conferir)
        inteiro = not falhas_locais
        nao_entregues = [f.split(" (", 1)[0] for f in falhas_locais]
        if not inteiro:
            _log(f"{motivo}: manifesto incompleto ({len(falhas_locais)} falha(s) local(is)) - nada será "
                 f"apagado no servidor nesta rodada")
            for falha in falhas_locais[:20]:
                _log(f"  local: {falha}")
        por_caminho = {i["caminho"]: i for i in itens}
        resposta = cliente.post("/api/sync/manifesto", {"arquivos": itens, "completo": inteiro})  # só calcula
        faltam = [c for c in resposta.get("faltam", []) if c in por_caminho]
        documentos = [c for c in faltam if not c.startswith("painel_dados/")]
        publicacao = [c for c in faltam if c.startswith("painel_dados/")]
        enviados, bytes_, falhas = 0, 0, []
        rede_caiu = False

        def enviar(caminho: str) -> bool:
            nonlocal enviados, bytes_, rede_caiu
            item = por_caminho[caminho]
            local = RAIZ / Path(*caminho.split("/"))
            if falhar_em and falhar_em in caminho:
                falhas.append(f"{caminho}: falha forçada (ensaio --ensaio-falhar-em)")
                return False
            try:
                st = local.stat()
                if (st.st_size, st.st_mtime_ns) != (item["tamanho"], item["mtime_ns"]):
                    falhas.append(f"{caminho}: mudou durante o envio (vai na próxima)")
                    return False
                sha = item.get("sha256") or _sha256(local)  # sempre do conteúdo de agora, nunca de cache
                cliente.enviar_arquivo(caminho, local, item["tamanho"], item["mtime_ns"], sha)
                enviados += 1
                bytes_ += item["tamanho"]
                os.utime(trava)  # sinal de vida para a trava não parecer abandonada
                return True
            except cliente_servidor.ServidorIndisponivel as exc:
                falhas.append(f"{caminho}: {exc}")
                rede_caiu = "HTTP" not in str(exc)  # rede caiu: não adianta insistir agora
            except OSError as exc:
                falhas.append(f"{caminho}: {type(exc).__name__}")
            return False

        for caminho in documentos:
            if rede_caiu or not enviar(caminho):
                nao_entregues.append(caminho)
        if nao_entregues:  # alguma planilha ou documento não chegou: a listagem fica para a próxima
            if publicacao:
                _log(f"{motivo}: publicação adiada ({', '.join(c.split('/')[-1] for c in publicacao)}) - "
                     f"{len(nao_entregues)} arquivo(s) da rodada não chegaram ao servidor")
            nao_entregues += publicacao
        else:
            for caminho in publicacao:
                if rede_caiu or not enviar(caminho):
                    nao_entregues.append(caminho)
        apagados = []
        if inteiro and not nao_entregues:  # por último: só depois de todos os envios e da publicação
            sem_sha = [{k: v for k, v in i.items() if k != "sha256"} for i in itens]
            fim = cliente.post("/api/sync/concluir", {"arquivos": sem_sha, "completo": True})
            apagados = fim.get("apagados") or []
            if fim.get("recusado"):
                _log(f"{motivo}: exclusões recusadas pelo servidor - {fim['recusado']}")
        elif resposta.get("sobrando"):
            _log(f"{motivo}: exclusões adiadas ({len(resposta['sobrando'])} arquivo(s) que sumiram do PC) - "
                 "a rodada não terminou inteira")
        _log(f"{motivo}: {'conferência completa (sha256), ' if conferir else ''}{len(itens)} no manifesto, "
             f"{len(faltam)} a enviar, {enviados} enviados ({bytes_ / 2**20:.1f} MB), {len(apagados)} apagados no "
             f"servidor, {len(falhas)} falha(s), {time.monotonic() - comeco:.0f} s")
        for falha in falhas[:20]:
            _log(f"  falhou: {falha}")
        if backup_do_dia_pendente():
            baixar_backup(cliente)
        ok = inteiro and not nao_entregues
        if ok and conferir:
            try:
                (PASTA_DADOS / "sync_conferencia.txt").write_text(datetime.now().strftime("%Y-%m-%d"),
                                                                  encoding="utf-8")
            except OSError:
                pass
        return Resultado(ok=ok, nao_entregues=nao_entregues)
    except cliente_servidor.ServidorIndisponivel as exc:
        _log(f"{motivo}: servidor indisponível - {exc}")
        return Resultado(ok=False, erro=str(exc))
    except Exception as exc:  # nunca derruba a varredura, o funil nem o executor
        _log(f"{motivo}: erro inesperado\n{traceback.format_exc()}")
        return Resultado(ok=False, erro=type(exc).__name__)
    finally:
        trava.unlink(missing_ok=True)


def sincronizar(motivo: str, esperar_s: float = 0, cliente=None, completo: bool | None = None) -> bool | None:
    """True = tudo entregue; False = falhou ou ficou algo (já no log); None =
    modo local (nada a fazer). Nunca levanta exceção."""
    r = sincronizar_detalhado(motivo, esperar_s, cliente, completo)
    return None if r.local else r.ok


def comparar_com_o_servidor(cliente=None, saida=None) -> int:
    """Ensaio: compara o PC com o servidor - contagem, tamanho, mtime e sha256 de
    cada arquivo - e mostra as diferenças. Não envia nem apaga nada. Devolve o
    número de diferenças (0 = batem)."""
    saida = saida or sys.stdout
    if cliente is None:
        try:
            if cliente_servidor.modo("envio") == "local":
                print("O envio está em modo local (SECRETARIA_MODO_ENVIO): não há servidor para comparar.",
                      file=saida)
                return 1
        except cliente_servidor.ConfiguracaoInvalida as exc:
            print(f"Configuração do servidor inválida: {exc}", file=saida)
            return 1
        cliente = cliente_servidor.Cliente()
    itens, falhas = manifesto(com_sha=True)
    r = cliente.post("/api/sync/manifesto", {"arquivos": itens, "completo": False, "apagar": False})
    ausentes = set(r.get("ausentes") or [])
    diferentes = [c for c in r.get("faltam") or [] if c not in ausentes]
    sobrando = list(r.get("sobrando") or [])
    total_mb = sum(i["tamanho"] for i in itens) / 2**20
    print(f"PC: {len(itens)} arquivos ({total_mb:.1f} MB)  |  servidor: {r.get('no_servidor', '?')} arquivos",
          file=saida)
    for titulo, lista in (("faltam no servidor", sorted(ausentes)),
                          ("diferentes (tamanho, data ou sha256)", sorted(diferentes)),
                          ("só no servidor (sumiram do PC)", sobrando),
                          ("o PC não conseguiu ler", falhas)):
        print(f"{titulo}: {len(lista)}", file=saida)
        for caminho in lista[:50]:
            print(f"  {caminho}", file=saida)
        if len(lista) > 50:
            print(f"  ... e mais {len(lista) - 50}", file=saida)
    total = len(ausentes) + len(diferentes) + len(sobrando) + len(falhas)
    print("BATEM: o servidor tem exatamente os arquivos do PC." if total == 0 else f"{total} diferença(s).",
          file=saida)
    return total


def main() -> int:
    argv = sys.argv[1:]
    if "--comparar" in argv:
        try:
            return 0 if comparar_com_o_servidor() == 0 else 1
        except cliente_servidor.ServidorIndisponivel as exc:
            print(f"Servidor indisponível: {exc}")
            return 1
    completo = "--completo" in argv
    falhar_em = argv[argv.index("--ensaio-falhar-em") + 1] if "--ensaio-falhar-em" in argv[:-1] else None
    motivo = "tarefa" + (" (--completo)" if completo else "") + (f" (ensaio: falhar em {falhar_em})" if falhar_em else "")
    r = sincronizar_detalhado(motivo, completo=True if completo else None, falhar_em=falhar_em)
    if falhar_em:
        print(f"ensaio: {len(r.nao_entregues)} não entregue(s); a listagem "
              f"{'NÃO foi publicada' if any(c.startswith('painel_dados/') for c in r.nao_entregues) else 'não precisava subir'}"
              f"; veja o log sync-*.txt e rode --comparar.")
    return 0 if (r.local or r.ok) else 1


if __name__ == "__main__":
    sys.exit(main())
