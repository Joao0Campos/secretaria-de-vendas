"""
Executor da fila de reanálise ("Reanalisar agora" do painel)
=============================================================

PARA QUE SERVE (decisão do João, 19/10/2026): o painel grava pedidos de
reanálise na tabela pedidos_reanalise do painel.db, e ESTE processo os atende.
O site continua sem credenciais do CV e da IA - quem chama o CV e a IA é só
este executor (e a varredura), o que permite à TI rodar os dois com contas
diferentes no servidor.

COMO FUNCIONA:
1. Residente: olha a fila a cada INTERVALO_S (15 s).
2. Para cada pedido, pega a MESMA trava da varredura (painel_dados/varredura.trava),
   com modo "reanalise". Trava ocupada (varredura diária, repescagem, funil):
   o pedido continua "na_fila" e o painel mostra o motivo da espera.
3. Analisa com a MESMA _analisar_reserva() da varredura: busca direta no CV
   antes (reserva que saiu do funil vira "fora_do_funil"), download, leitura da
   IA SÓ do documento novo ou trocado (o cache da leitura vale igual - não
   existe "forçar releitura" aqui) e as regras.
4. Grava o resultado no pedido e solta a trava. UM pedido por volta, com
   INTERVALO_S de folga entre um e outro: a varredura das 7h, que espera a
   reanálise, entra na primeira folga e nunca espera a fila inteira.
5. Ao subir (e a cada volta), pedido "em_andamento" há mais de 20 min é de um
   executor que morreu no meio: vira "falhou" com "executor reiniciado".

Log em painel_dados/logs/reanalise-AAAA-MM-DD.txt. NADA vai para
varreduras.jsonl nem para o marcador do dia: reanálise não é varredura.

OPÇÃO B (20/10/2026: só o site no servidor). Com SECRETARIA_MODO_EXECUTOR=remoto
(e o envio remoto; ver cliente_servidor.modo), o executor NÃO abre o painel.db (ele fica só no
servidor): a fila vem pela API do site (FilaRemota), com a chave. A cada volta
ele manda o modo da trava do PC (o site mostra "aguardando a varredura" ou
"executor desligado"), e só dá o pedido por concluído DEPOIS de enviar a
planilha nova ao servidor (sincronizar_servidor.py). Sem as variáveis: modo
local, como antes.

⚠️ SOMENTE LEITURA no CV. Não altera nada lá.

Como rodar:
    python executor_reanalise.py            -> residente (Ctrl+C para parar)
    python executor_reanalise.py --uma-vez  -> atende no máximo UM pedido e sai
No Windows, executor_reanalise.pyw sobe sem janela no logon (Agendador).
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import analisar_documentos  # noqa: E402
import cliente_servidor  # noqa: E402
import mvp_v2_reserva  # noqa: E402
import sincronizar_servidor  # noqa: E402
import varredura_diaria as vd  # noqa: E402
from painel import config as config_painel  # noqa: E402
from painel import funil as funil_painel  # noqa: E402
from painel.banco import (  # noqa: E402
    PEDIDO_CONCLUIDO,
    PEDIDO_FALHOU,
    PEDIDO_FORA_DO_FUNIL,
    PEDIDO_TIPO_FUNIL,
    Banco,
)

INTERVALO_S = 15
NOME_DA_INSTANCIA = "SecretariaDeVendasVIC-ExecutorReanalise"
SINAL_DE_VIDA_S = 20        # durante uma análise longa (o site acha o executor desligado após 60 s)
ESPERA_DO_ENVIO_S = 300     # pela sincronização em andamento, antes de concluir o pedido

# Opção B: o pedido só conclui se TODAS as pastas da reserva chegaram ao servidor.
# Se o envio falhou, o pedido vira "falhou" com este motivo - e não volta para a
# fila: voltar refaria o CV e a IA e seguraria a trava do PC a cada volta enquanto
# a rede estiver fora (a varredura das 7h esperaria), sem ganho - a análise já
# está no disco do PC, e a sincronização de 5 min a entrega sozinha. O motivo
# diz isso, e "Reanalisar agora" continua disponível.
MOTIVO_ENVIO_FALHOU = ("a análise terminou no PC, mas a planilha não chegou ao servidor (envio falhou); "
                       "ela sobe na próxima sincronização, em até 5 min - se não aparecer, peça de novo")
MOTIVO_LISTAGEM_NAO_CHEGOU = ("a listagem nova não chegou ao servidor (envio falhou) - busque de novo em "
                              "alguns minutos")


# ------------------------------------------------------------------ log
@contextlib.contextmanager
def _log_do_dia(eco: bool = True):
    """Tudo o que for impresso vai para o log do dia (e para o terminal, com
    eco). Aberto só quando há o que registrar: a fila vazia, olhada a cada 15 s,
    não escreve nada."""
    vd.PASTA_LOGS.mkdir(parents=True, exist_ok=True)
    caminho = vd.PASTA_LOGS / f"reanalise-{datetime.now():%Y-%m-%d}.txt"
    with caminho.open("a", encoding="utf-8") as arquivo:
        destinos = [arquivo]
        if eco and sys.__stdout__ is not None:
            destinos.append(sys.__stdout__)
        saida = vd.Saida(*destinos)
        with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
            yield


def _agora() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ------------------------------------------------------------------ um pedido
def _analisar(idreserva: int) -> tuple[str, str | None, str | None]:
    """(status final, resultado, erro) da reanálise de uma reserva, pela MESMA
    _analisar_reserva() da varredura."""
    # O cache da listagem de associados é por PROCESSO (mvp_v2_reserva): certo
    # para a varredura, que dura uma rodada; errado aqui, que fica no ar dias -
    # um 2º comprador incluído depois não apareceria. Esvaziado a cada pedido.
    mvp_v2_reserva._LISTAGEM_DE_ASSOCIADOS.clear()
    analisar_documentos.ORIGEM_DO_CONSUMO = "reanalise"  # o consumo da IA sai marcado como reanálise
    listagem = vd._ler_listagem()
    reservas = listagem.get("reservas") or {}
    situacao = str((reservas.get(str(idreserva)) or {}).get("situacao") or "").strip()
    rodada = {"reservas_ok": 0, "pastas": 0, "falhas": []}
    try:
        cfg = vd.carregar_config()
        resultado = vd._analisar_reserva(cfg, idreserva, situacao, rodada)
    except (Exception, SystemExit) as e:  # _analisar_reserva já captura o da reserva; isto é o resto
        traceback.print_exc()
        return PEDIDO_FALHOU, None, str(e).split(":", 1)[0][:200]
    if resultado == "ok":
        return PEDIDO_CONCLUIDO, f"{rodada['pastas']} pasta(s) analisada(s)", None
    if resultado == "fora":
        nova = rodada["fora_do_funil"][-1]["situacao"]
        # Como a varredura faz no fim da rodada: corrige a cópia local com o que
        # a busca direta viu (este processo está com a trava).
        if str(idreserva) in reservas:
            reservas[str(idreserva)]["situacao"] = nova
            listagem["reservas"] = reservas
            vd._gravar_listagem(listagem)
        return PEDIDO_FORA_DO_FUNIL, f"saiu do funil: agora em {nova}", None
    return PEDIDO_FALHOU, None, (rodada["falhas"][-1]["erro"] if rodada["falhas"] else "falha sem detalhe")


# ------------------------------------------------------------------ fila pela API (opção B)
class FilaRemota:
    """A mesma interface que o Executor usa do Banco, pela API do site no
    servidor. Os órfãos são recuperados pelo próprio site (a cada sinal de vida)."""

    remota = True

    def __init__(self, cliente, eco: bool = True):
        self.cliente = cliente
        self.eco = eco
        self._falha_anunciada: str | None = None  # loga a queda da rede uma vez só

    def _modo_da_trava(self) -> str | None:
        return (vd._ler_trava() or {}).get("modo")

    def sinal_de_vida(self, modo: str | None = None) -> bool:
        """Manda o modo da trava do PC; devolve se há pedido na fila. Rede fora:
        False, com uma linha no log por queda."""
        try:
            r = self.cliente.post("/api/executor/estado", {
                "modo": modo or self._modo_da_trava(),
                # o disjuntor da IA no PC (painel/ia_falhas.py), para o banner do admin
                "ia": analisar_documentos.ia_falhas.estado(analisar_documentos.ARQUIVO_ESTADO_IA)})
        except cliente_servidor.ServidorIndisponivel as exc:
            if self._falha_anunciada != str(exc):
                with _log_do_dia(self.eco):
                    print(f"{_agora()} servidor indisponível ({exc}) - tentando de novo a cada {INTERVALO_S} s")
                self._falha_anunciada = str(exc)
            return False
        if self._falha_anunciada:
            with _log_do_dia(self.eco):
                print(f"{_agora()} servidor de volta")
            self._falha_anunciada = None
        return bool(r.get("ha_pedido"))

    @contextlib.contextmanager
    def sinal_de_vida_em_segundo_plano(self):
        parar = threading.Event()

        def bater():
            while not parar.wait(SINAL_DE_VIDA_S):
                self.sinal_de_vida("reanalise")
        fio = threading.Thread(target=bater, daemon=True)
        fio.start()
        try:
            yield
        finally:
            parar.set()
            fio.join(timeout=5)

    def recuperar_pedidos_orfaos(self) -> list[int]:
        return []

    def ha_pedido_na_fila(self) -> bool:
        return self.sinal_de_vida()

    def pegar_proximo_pedido(self) -> dict | None:
        return self.cliente.post("/api/executor/proximo").get("pedido")

    def concluir_pedido(self, id_pedido: int, status: str, resultado: str | None = None,
                        erro: str | None = None) -> bool:
        for tentativa in range(3):
            try:
                self.cliente.post("/api/executor/concluir",
                                  {"id": id_pedido, "status": status, "resultado": resultado, "erro": erro})
                return True
            except cliente_servidor.ServidorIndisponivel as exc:
                if "HTTP 409" in str(exc):  # o site já o deu por órfão
                    return False
                if tentativa == 2:
                    raise
                time.sleep(10)
        return False

    def enfileirar_da_busca(self, pedido: dict, ids: list[int]) -> dict:
        return self.cliente.post("/api/executor/enfileirar", {"pedido_id": pedido["id"], "ids": ids})


class Executor:
    def __init__(self, banco, eco: bool = True, saidas: Path | None = None):
        """'banco': o painel.db (modo local) ou uma FilaRemota (opção B)."""
        self.banco = banco
        self.remota = bool(getattr(banco, "remota", False))
        self.eco = eco
        self.saidas = Path(saidas) if saidas else RAIZ / "saidas"
        self._esperando_desde: str | None = None  # para logar a espera uma vez só

    def recuperar_orfaos(self) -> list[int]:
        ids = self.banco.recuperar_pedidos_orfaos()
        if ids:
            with _log_do_dia(self.eco):
                print(f"{_agora()} pedido(s) {', '.join(map(str, ids))} em andamento há mais de 20 min "
                      f"(executor anterior morreu no meio): marcados como falhou")
        return ids

    def atender_um(self) -> str | None:
        """Atende no máximo UM pedido. Devolve o status final dele,
        "trava_ocupada" (o pedido continua na fila) ou None (fila vazia)."""
        self.recuperar_orfaos()
        if not self.banco.ha_pedido_na_fila():
            self._esperando_desde = None
            return None
        token = vd._pegar_trava("reanalise")
        if token is None:
            dono = (vd._ler_trava() or {}).get("modo") or "outro processo"
            if self._esperando_desde != dono:
                with _log_do_dia(self.eco):
                    print(f"{_agora()} há pedido na fila, mas a trava está com: {dono} - esperando")
                self._esperando_desde = dono
            return "trava_ocupada"
        self._esperando_desde = None
        try:
            pedido = self.banco.pegar_proximo_pedido()
            if pedido is None:
                return None
            vida = self.banco.sinal_de_vida_em_segundo_plano() if self.remota else contextlib.nullcontext()
            with _log_do_dia(self.eco), vida:
                comeco = time.monotonic()
                print(f"\n{_agora()} ===== pedido {pedido['id']}: reserva {pedido['idreserva']} "
                      f"(pedido por {pedido['pedido_por']}, origem {pedido['origem']}, às "
                      f"{pedido['pedido_em'][11:16]}) =====")
                if pedido.get("tipo") == PEDIDO_TIPO_FUNIL:
                    status, resultado, erro = self._buscar_reservas_novas(pedido)
                else:
                    status, resultado, erro = _analisar(pedido["idreserva"])
                if self.remota and pedido.get("tipo") != PEDIDO_TIPO_FUNIL and status != PEDIDO_FALHOU:
                    # o site só mostra "concluído" quando a planilha nova já está lá
                    envio = sincronizar_servidor.sincronizar_detalhado(f"reanálise {pedido['idreserva']}",
                                                                      esperar_s=ESPERA_DO_ENVIO_S)
                    if not envio.reserva_entregue(pedido["idreserva"]):
                        status, resultado, erro = PEDIDO_FALHOU, None, MOTIVO_ENVIO_FALHOU
                self.banco.concluir_pedido(pedido["id"], status, resultado, erro)
                print(f"{_agora()} pedido {pedido['id']}: {status}"
                      + (f" - {resultado}" if resultado else "") + (f" - {erro}" if erro else "")
                      + f" ({time.monotonic() - comeco:.0f} s)")
            return status
        finally:
            vd._soltar_trava(token)

    def _buscar_reservas_novas(self, pedido: dict) -> tuple[str, str | None, str | None]:
        """Pedido de tipo 'funil' ("Buscar reservas novas", 19/10/2026): atualiza
        a listagem do CV agora, com a trava que este executor já segura (o
        mesmo miolo do --atualizar-funil), e enfileira a análise de TODAS as
        reservas do funil ainda sem planilha, com a origem e o autor do pedido.
        O teto da fila vale: o que não coube fica registrado e a tela avisa.
        'resultado' é JSON, lido pelo painel para o progresso."""
        texto, codigo = vd.rodar_funil_com_a_trava()
        print(f"{_agora()} {texto}")
        if codigo:
            return PEDIDO_FALHOU, None, texto.split(" - ", 1)[-1]
        listagem = funil_painel.Listagem(vd.PASTA_DADOS)
        pasta = self.saidas / "reserva"
        analisadas = {int(m.group(1)) for d in (pasta.iterdir() if pasta.is_dir() else [])
                      if (m := re.search(r"-reserva-(\d+)$", d.name))}
        sem_analise = listagem.no_funil_sem_analise(analisadas)
        ids = [item["id"] for item in sem_analise]  # mais novas primeiro: se o teto cortar, cortam as mais velhas
        if self.remota:
            # O site confere contra a listagem DELE: manda a nova antes de enfileirar.
            envio = sincronizar_servidor.sincronizar_detalhado("busca de reservas novas",
                                                              esperar_s=ESPERA_DO_ENVIO_S)
            if envio.erro or "painel_dados/reservas_cv.json" in envio.nao_entregues:
                return PEDIDO_FALHOU, None, MOTIVO_LISTAGEM_NAO_CHEGOU
            fila = self.banco.enfileirar_da_busca(pedido, ids)
        else:
            fila = self.banco.enfileirar_varias(ids, pedido["origem"], pedido["pedido_por"], listagem.ids())
        print(f"{_agora()} busca: {len(sem_analise)} sem análise no funil; {len(fila['enfileiradas'])} na fila, "
              f"{len(fila['ja_abertas'])} já estavam, {len(fila['fora_pelo_teto'])} de fora pelo teto")
        return PEDIDO_CONCLUIDO, json.dumps({
            "funil": texto, "sem_analise": len(sem_analise), "enfileiradas": fila["enfileiradas"],
            "ja_abertas": fila["ja_abertas"], "fora_pelo_teto": fila["fora_pelo_teto"],
            "recusadas": fila["recusadas"],
        }), None

    def rodar_para_sempre(self) -> None:
        with _log_do_dia(self.eco):
            print(f"\n{_agora()} executor de reanálise iniciado (fila a cada {INTERVALO_S} s)")
        while True:
            try:
                self.atender_um()
            except Exception:  # erro do banco ou do disco: registra e segue na próxima volta
                with _log_do_dia(self.eco):
                    print(f"{_agora()} erro inesperado no executor:")
                    traceback.print_exc()
            # Sempre a folga, também depois de atender: é nela que a varredura
            # (que espera a reanálise) consegue a trava.
            time.sleep(INTERVALO_S)


# ------------------------------------------------------------------ instância única
@contextlib.contextmanager
def instancia_unica(nome: str = NOME_DA_INSTANCIA):
    """True se este é o ÚNICO executor; False se já há outro rodando.

    Windows: um mutex com nome (CreateMutexW). Ele some sozinho quando o
    processo morre - não deixa arquivo velho para trás, como uma trava em disco
    deixaria. "Global\\" vale para todas as sessões do servidor; se a conta não
    puder criar nesse espaço, cai para "Local\\" (a sessão do usuário).
    Fora do Windows: flock num arquivo em painel_dados/, também solto pelo
    sistema quando o processo morre."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        alca = None
        for espaco in ("Global\\", "Local\\"):
            alca = k32.CreateMutexW(None, False, espaco + nome)
            erro = ctypes.get_last_error()
            if alca:
                break
            if erro != 5:  # 5 = acesso negado ao espaço Global: tenta o Local
                break
        try:
            yield bool(alca) and erro != 183  # 183 = ERROR_ALREADY_EXISTS
        finally:
            if alca:
                k32.CloseHandle(alca)
        return
    import fcntl
    vd.PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    with open(vd.PASTA_DADOS / f"{nome}.lock", "a") as arquivo:
        try:
            fcntl.flock(arquivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def main(argv: list[str] | None = None, eco: bool = True) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:  # modo explícito: configuração parcial não cai no painel.db local
        remoto = cliente_servidor.modo("executor") == "remoto"
    except cliente_servidor.ConfiguracaoInvalida as exc:
        with _log_do_dia(eco):
            print(f"{_agora()} executor NÃO subiu: {exc}")
        return 2
    # Uma instância por MODO: no ensaio, o executor local (residente, painel.db do
    # PC) continua no ar e UM executor remoto (--uma-vez) atende o pedido de teste
    # do servidor. A trava do PC (varredura.trava) serializa as análises dos dois.
    with instancia_unica(NOME_DA_INSTANCIA + ("-Remoto" if remoto else "")) as unico:
        if not unico:
            print("Já há um executor de reanálise rodando. Saindo sem fazer nada.")
            return 0
        cfg = config_painel.carregar()
        # Opção B: no modo remoto, o painel.db NÃO é aberto aqui.
        try:
            fila = FilaRemota(cliente_servidor.Cliente(), eco=eco) if remoto else Banco(cfg.db)
        except cliente_servidor.ServidorIndisponivel as exc:
            with _log_do_dia(eco):
                print(f"{_agora()} executor NÃO subiu: {exc}")
            return 2
        executor = Executor(fila, eco=eco, saidas=cfg.saidas)
        if "--uma-vez" in argv:
            status = executor.atender_um()
            print(f"--uma-vez: {status or 'fila vazia'}")
            return 0
        try:
            executor.rodar_para_sempre()
        except KeyboardInterrupt:
            print("executor parado.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
