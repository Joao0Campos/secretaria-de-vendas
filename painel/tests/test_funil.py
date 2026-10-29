"""Testes do --atualizar-funil (Parte A, 19/10/2026) - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_funil.py -v

O CV não é chamado: atualizar_listagem(), carregar_config() e a análise são
substituídos por versões falsas, e a blindagem bloqueia rede e escrita na pasta
do projeto. Trava, listagem, marcador, varreduras.jsonl e logs vão para um
diretório temporário.
"""
from __future__ import annotations

import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parents[2]
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

import blindagem  # noqa: E402,F401  (primeiro import do projeto)

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import varredura_diaria as vd  # noqa: E402

ORIGINAIS = {nome: getattr(vd, nome) for nome in (
    "ARQ_TRAVA", "ARQ_ULTIMA_OK", "ARQ_REGISTRO", "ARQ_LISTAGEM", "PASTA_LOGS",
    "carregar_config", "atualizar_listagem", "processar_reserva_completa",
    "processar_pasta_cliente", "situacao_atual_no_cv", "ESPERA_PELA_TRAVA", "INTERVALO_DA_ESPERA_S",
    "_pegar_trava", "TENTATIVAS_SE_A_TRAVA_SUMIR")}

LISTAGEM_ANTES = {
    "1": {"idreserva": 1, "situacao": "Nova Reserva"},
    "2": {"idreserva": 2, "situacao": "Venda Validada"},
}
# Depois: 1 igual, 2 mudou (cancelada), 3 entrou no funil -> 2 alteradas, 2 no funil.
LISTAGEM_DEPOIS = {
    "1": {"idreserva": 1, "situacao": "Nova Reserva"},
    "2": {"idreserva": 2, "situacao": "Cancelada"},
    "3": {"idreserva": 3, "situacao": "Reserva Aprovada"},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.dados = Path(tempfile.mkdtemp())
        vd.ARQ_TRAVA = self.dados / "varredura.trava"
        vd.ARQ_ULTIMA_OK = self.dados / "ultima_varredura_ok.txt"
        vd.ARQ_REGISTRO = self.dados / "varreduras.jsonl"
        vd.ARQ_LISTAGEM = self.dados / "reservas_cv.json"
        vd.PASTA_LOGS = self.dados / "logs"
        self.chamadas = {"listagem": 0, "reservas": []}
        vd.carregar_config = lambda: {}
        vd.situacao_atual_no_cv = lambda cfg, idr: None
        vd.processar_pasta_cliente = lambda *a, **k: None

        def listagem_falsa(cfg):
            # Como a de verdade: grava reservas_cv.json e devolve as reservas.
            self.chamadas["listagem"] += 1
            vd._gravar_listagem({"atualizado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 "ultima_referencia": "2026-10-19 10:00:00",
                                 "reservas": json.loads(json.dumps(LISTAGEM_DEPOIS))})
            return json.loads(json.dumps(LISTAGEM_DEPOIS)), "incremental (teste)"
        vd.atualizar_listagem = listagem_falsa

        def reserva_falsa(idr, devolver_associados=False):
            self.chamadas["reservas"].append(idr)
            return [], [], None
        vd.processar_reserva_completa = reserva_falsa

        vd._gravar_listagem({"atualizado_em": "2026-10-19 09:00:00",
                             "ultima_referencia": "2026-10-19 08:00:00", "reservas": LISTAGEM_ANTES})
        # Marcador de HOJE com pendentes: o funil não pode disparar a repescagem.
        vd._gravar_marcador({"concluida_em": datetime.now().strftime("%Y-%m-%d 07:29:29"),
                             "reservas_alvo": 3, "reservas_ok": 2, "pastas": 2,
                             "pendentes": {"2": {"tentativas": 1, "erro": "Erro 422"}}})
        self.marcador_antes = vd.ARQ_ULTIMA_OK.read_bytes()
        self.listagem_antes = vd.ARQ_LISTAGEM.read_bytes()

    def tearDown(self):
        for nome, valor in ORIGINAIS.items():
            setattr(vd, nome, valor)
        shutil.rmtree(self.dados, ignore_errors=True)

    def trava_de(self, modo: str | None) -> bytes:
        """Trava VIVA (o PID é o deste processo, criado antes do início)."""
        info = {"pid": os.getpid(), "token": "de-outro", "inicio": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")}
        if modo:
            info["modo"] = modo
        conteudo = json.dumps(info).encode()
        vd.ARQ_TRAVA.write_bytes(conteudo)
        return conteudo

    def linhas_do_log(self) -> list[str]:
        logs = list(vd.PASTA_LOGS.glob("*.txt")) if vd.PASTA_LOGS.exists() else []
        return [l for p in logs for l in p.read_text(encoding="utf-8").splitlines()]

    def quieto(self, funcao, *a, **k):
        with contextlib.redirect_stdout(io.StringIO()) as saida:
            r = funcao(*a, **k)
        return r, saida.getvalue()


class AtualizarFunil(Base):
    def test_trava_ocupada_sai_com_0_sem_gravar_nada(self):
        for modo in ("varredura", "repescagem", "funil", None):
            with self.subTest(dono=modo):
                trava = self.trava_de(modo)
                codigo, saida = self.quieto(vd.atualizar_funil)
                self.assertEqual(codigo, 0)
                self.assertEqual(saida, "", "trava ocupada: sai calado")
                self.assertEqual(self.chamadas["listagem"], 0, "não chama o CV")
                self.assertEqual(vd.ARQ_LISTAGEM.read_bytes(), self.listagem_antes)
                self.assertEqual(vd.ARQ_ULTIMA_OK.read_bytes(), self.marcador_antes)
                self.assertFalse(vd.ARQ_REGISTRO.exists())
                self.assertEqual(self.linhas_do_log(), [], "nem linha no log")
                self.assertEqual(vd.ARQ_TRAVA.read_bytes(), trava, "a trava dos outros fica")

    def test_livre_atualiza_e_escreve_uma_linha(self):
        codigo, _ = self.quieto(vd.atualizar_funil)
        self.assertEqual(codigo, 0)
        self.assertEqual(self.chamadas["listagem"], 1)
        linhas = self.linhas_do_log()
        self.assertEqual(len(linhas), 1, linhas)
        self.assertRegex(linhas[0], r"^\d\d:\d\d:\d\d funil: 2 alteradas, 2 no funil \(\d+,\d s\)$")
        self.assertNotIn("=====", "\n".join(linhas))
        self.assertEqual(json.loads(vd.ARQ_LISTAGEM.read_text(encoding="utf-8"))["reservas"], LISTAGEM_DEPOIS)
        self.assertFalse(vd.ARQ_TRAVA.exists(), "solta a trava")

    def test_nao_grava_jsonl_nem_mexe_no_marcador_nem_repesca(self):
        for _ in range(3):
            self.quieto(vd.atualizar_funil)
        self.assertFalse(vd.ARQ_REGISTRO.exists())
        self.assertEqual(vd.ARQ_ULTIMA_OK.read_bytes(), self.marcador_antes)
        self.assertEqual(self.chamadas["reservas"], [], "a repescagem não roda")
        self.assertEqual(len(self.linhas_do_log()), 3)

    def test_falha_do_cv_registra_e_solta_a_trava(self):
        def cv_fora(cfg):
            raise SystemExit("Erro 500 na listagem incremental (página 1): <corpo cru>")
        vd.atualizar_listagem = cv_fora
        codigo, _ = self.quieto(vd.atualizar_funil)
        self.assertEqual(codigo, 2)
        linhas = self.linhas_do_log()
        self.assertEqual(len(linhas), 1)
        self.assertIn("funil: FALHOU - Erro 500 na listagem incremental (página 1)", linhas[0])
        self.assertNotIn("corpo cru", linhas[0])
        self.assertFalse(vd.ARQ_TRAVA.exists())
        self.assertFalse(vd.ARQ_REGISTRO.exists())
        self.assertEqual(vd.ARQ_ULTIMA_OK.read_bytes(), self.marcador_antes)

    def test_main_com_a_opcao(self):
        argv = sys.argv
        sys.argv = ["varredura_diaria.py", "--atualizar-funil"]
        try:
            codigo, _ = self.quieto(vd.main)
        finally:
            sys.argv = argv
        self.assertEqual(codigo, 0)
        self.assertEqual(self.chamadas["listagem"], 1)
        self.assertEqual(self.chamadas["reservas"], [])
        self.assertEqual(len(self.linhas_do_log()), 1)

    def test_grava_a_trava_com_o_modo(self):
        vistos = []

        def listagem_que_olha_a_trava(cfg):
            vistos.append(vd._ler_trava())
            return {}, "teste"
        vd.atualizar_listagem = listagem_que_olha_a_trava
        self.quieto(vd.atualizar_funil)
        self.assertEqual(vistos[0]["modo"], "funil")
        self.assertEqual(vistos[0]["pid"], os.getpid())


class VarreduraEsperaOFunil(Base):
    """A varredura das 7h não pode perder o dia porque o funil das 7h00 está
    com a trava: espera ele soltar."""

    def setUp(self):
        super().setUp()
        vd.ARQ_ULTIMA_OK.unlink()  # dia ainda não feito
        vd.INTERVALO_DA_ESPERA_S = 0.1

    def soltar_depois(self, segundos: float, depois=None):
        def soltar():
            time.sleep(segundos)
            vd.ARQ_TRAVA.unlink(missing_ok=True)
            if depois:
                depois()
        t = threading.Thread(target=soltar)
        t.start()
        return t

    def test_espera_o_funil_e_roda(self):
        self.trava_de("funil")
        t = self.soltar_depois(0.5)
        codigo, saida = self.quieto(vd.rodar)
        t.join()
        self.assertEqual(codigo, 0)
        self.assertIn("A trava está com o funil - esperando", saida)
        self.assertEqual(sorted(self.chamadas["reservas"]), [1, 3])
        self.assertTrue(vd.ARQ_ULTIMA_OK.exists(), "o dia ficou feito")

    def test_repescagem_tambem_espera(self):
        vd._gravar_marcador({"concluida_em": datetime.now().strftime("%Y-%m-%d 07:29:29"),
                             "reservas_alvo": 3, "reservas_ok": 2, "pastas": 2,
                             "pendentes": {"3": {"tentativas": 1, "erro": "Erro 422"}}})
        self.trava_de("funil")
        t = self.soltar_depois(0.5)
        codigo, saida = self.quieto(vd.rodar)
        t.join()
        self.assertEqual(codigo, 0)
        self.assertIn("repescagem: 1 reserva(s)", saida)
        self.assertEqual(self.chamadas["reservas"], [3])

    def test_nao_espera_outra_varredura(self):
        for modo in ("varredura", "repescagem", None):
            with self.subTest(dono=modo):
                self.trava_de(modo)
                comeco = time.monotonic()
                codigo, saida = self.quieto(vd.rodar)
                self.assertEqual(codigo, 1)
                self.assertLess(time.monotonic() - comeco, 2)
                self.assertNotIn("esperando", saida)
                self.assertEqual(self.chamadas["reservas"], [])

    def test_espera_tem_limite(self):
        vd.ESPERA_PELA_TRAVA = timedelta(seconds=0.5)
        self.trava_de("funil")
        comeco = time.monotonic()
        codigo, _ = self.quieto(vd.rodar)
        self.assertEqual(codigo, 1)
        self.assertLess(time.monotonic() - comeco, 3)
        self.assertEqual(self.chamadas["reservas"], [])

    def test_se_outro_disparo_fez_o_dia_enquanto_esperava_nao_roda_de_novo(self):
        self.trava_de("funil")

        def outro_fez_o_dia():
            vd._marcar_ok(datetime.now(), {"reservas_alvo": 2, "reservas_ok": 2, "pastas": 2, "falhas": []})
        t = self.soltar_depois(0.5, outro_fez_o_dia)
        codigo, saida = self.quieto(vd.rodar)
        t.join()
        self.assertEqual(codigo, 0)
        self.assertIn("já rodou hoje", saida)
        self.assertEqual(self.chamadas["reservas"], [])
        self.assertFalse(vd.ARQ_TRAVA.exists())

    def test_trava_some_entre_a_tentativa_e_a_leitura(self):
        """A corrida da revisão da Forja (19/10/2026): _pegar_trava() falha
        porque o funil ainda está com a trava, e o funil solta ANTES de
        _ler_trava(). A leitura volta None - não é outra varredura: a varredura
        tenta de novo na hora, pega e roda."""
        self.trava_de("funil")
        verdadeira = ORIGINAIS["_pegar_trava"]
        tentativas = []

        def pegar_e_o_funil_solta(modo="varredura"):
            token = verdadeira(modo)
            tentativas.append(token)
            if token is None:
                vd.ARQ_TRAVA.unlink()  # o funil soltou logo depois
            return token
        vd._pegar_trava = pegar_e_o_funil_solta
        comeco = time.monotonic()
        codigo, saida = self.quieto(vd.rodar)
        self.assertEqual(codigo, 0)
        self.assertEqual(tentativas[0], None, "a 1ª tentativa falhou de verdade")
        self.assertEqual(len(tentativas), 2, "pegou na 2ª, na hora")
        self.assertNotIn("esperando", saida, "não contou como espera")
        self.assertLess(time.monotonic() - comeco, 2)
        self.assertEqual(sorted(self.chamadas["reservas"]), [1, 3])
        self.assertTrue(vd.ARQ_ULTIMA_OK.exists(), "o dia ficou feito")

    def test_trava_vazia_de_quem_acabou_de_criar_nao_e_desistencia(self):
        """Trava recém-criada e ainda vazia ({}): também não prova outra
        varredura. Se ela some em seguida, a varredura pega."""
        vd.ARQ_TRAVA.write_bytes(b"")
        verdadeira = ORIGINAIS["_pegar_trava"]
        chamadas = []

        def pegar(modo="varredura"):
            chamadas.append(modo)
            if len(chamadas) == 2:
                vd.ARQ_TRAVA.unlink()  # quem estava criando desistiu/soltou
            return verdadeira(modo)
        vd._pegar_trava = pegar
        codigo, _ = self.quieto(vd.rodar)
        self.assertEqual(codigo, 0)
        self.assertEqual(sorted(self.chamadas["reservas"]), [1, 3])

    def test_trava_que_some_sempre_nao_vira_laco_infinito(self):
        vd.TENTATIVAS_SE_A_TRAVA_SUMIR = 5
        chamadas = []

        def nunca_pega(modo="varredura"):
            chamadas.append(modo)
            return None  # e não há trava no disco: _ler_trava() devolve None
        vd._pegar_trava = nunca_pega
        comeco = time.monotonic()
        codigo, _ = self.quieto(vd.rodar)
        self.assertEqual(codigo, 1)
        self.assertEqual(len(chamadas), 6, "1 + 5 tentativas na hora, e desiste")
        self.assertLess(time.monotonic() - comeco, 2)
        self.assertEqual(self.chamadas["reservas"], [])

    def test_forcar_continua_sem_esperar_marcador(self):
        self.trava_de("funil")
        t = self.soltar_depois(0.3)
        codigo, _ = self.quieto(vd.rodar, forcar=True)
        t.join()
        self.assertEqual(codigo, 0)
        self.assertEqual(sorted(self.chamadas["reservas"]), [1, 3])


if __name__ == "__main__":
    unittest.main()
