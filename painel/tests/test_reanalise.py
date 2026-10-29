"""Testes da fila de reanálise (Parte B, 19/10/2026) - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_reanalise.py -v

Reaproveita a Base do test_painel (mesma blindagem: sem rede, sem config.json,
sem escrita no projeto; planilhas sintéticas num diretório temporário). O CV e
a IA são substituídos logo ABAIXO de _analisar_reserva(): o executor roda a
função de verdade da varredura, e só a busca direta, o download e a leitura
são falsos.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import test_painel as tp  # noqa: E402  (carrega a blindagem antes do projeto)

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import executor_reanalise as ex  # noqa: E402
import mvp_v2_reserva  # noqa: E402
import varredura_diaria as vd  # noqa: E402
from painel import banco as banco_mod  # noqa: E402

ORIGINAIS = {nome: getattr(vd, nome) for nome in (
    "ARQ_TRAVA", "ARQ_ULTIMA_OK", "ARQ_REGISTRO", "ARQ_LISTAGEM", "PASTA_LOGS", "PASTA_DADOS",
    "carregar_config", "processar_reserva_completa", "processar_pasta_cliente", "situacao_atual_no_cv")}
RESERVAS = (34510, 34520, 34530)


class BaseFila(tp.Base):
    def setUp(self):
        super().setUp()
        self.dados = self.cfg.db.parent  # painel_dados/ do teste: banco, listagem, trava, logs
        vd.PASTA_DADOS = self.dados
        vd.ARQ_TRAVA = self.dados / "varredura.trava"
        vd.ARQ_ULTIMA_OK = self.dados / "ultima_varredura_ok.txt"
        vd.ARQ_REGISTRO = self.dados / "varreduras.jsonl"
        vd.ARQ_LISTAGEM = self.dados / "reservas_cv.json"
        vd.PASTA_LOGS = self.dados / "logs"
        self.escrever_listagem({str(i): {"idreserva": i, "situacao": "Nova Reserva"} for i in RESERVAS})
        self.cv = {"analisadas": [], "situacao": {}, "quebrar": set()}
        vd.carregar_config = lambda: {}
        vd.situacao_atual_no_cv = lambda cfg, idr: self.cv["situacao"].get(idr)

        def reserva_falsa(idr, devolver_associados=False):
            self.cv["analisadas"].append(idr)
            if idr in self.cv["quebrar"]:
                raise SystemExit(f"Erro 422 ao buscar documentos da reserva {idr}: corpo cru")
            return [Path(f"pasta-{idr}-titular"), Path(f"pasta-{idr}-2o")], [], None
        vd.processar_reserva_completa = reserva_falsa
        vd.processar_pasta_cliente = lambda *a, **k: None

    def tearDown(self):
        for nome, valor in ORIGINAIS.items():
            setattr(vd, nome, valor)
        super().tearDown()

    def escrever_listagem(self, reservas: dict):
        vd._gravar_listagem({"atualizado_em": "2026-10-19 12:00:00",
                             "ultima_referencia": "2026-10-19 11:59:00", "reservas": reservas})

    def trava_viva(self, modo: str | None) -> bytes:
        info = {"pid": os.getpid(), "token": "de-outro",
                "inicio": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")}
        if modo:
            info["modo"] = modo
        conteudo = json.dumps(info).encode()
        vd.ARQ_TRAVA.write_bytes(conteudo)
        return conteudo

    def pedir(self, idreserva, csrf, client=None):
        return (client or self.client).post("/api/reanalise", json={"idreserva": idreserva},
                                            headers={"X-CSRF-Token": csrf})

    def pedidos(self) -> list[dict]:
        con = sqlite3.connect(self.cfg.db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(l) for l in con.execute("SELECT * FROM pedidos_reanalise ORDER BY id")]
        finally:
            con.close()

    def executor(self) -> ex.Executor:
        return ex.Executor(self.banco, eco=False)


# ==========================================================================
# Rotas
# ==========================================================================
class Rotas(BaseFila):
    def test_sem_csrf_e_403(self):
        self.entrar()
        r = self.client.post("/api/reanalise", json={"idreserva": 34510})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/reanalise", json={"idreserva": 34510}, headers={"X-CSRF-Token": "errado"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.pedidos(), [])

    def test_sem_login_e_401(self):
        self.assertEqual(self.client.post("/api/reanalise", json={"idreserva": 34510}).status_code, 401)
        self.assertEqual(self.client.get("/api/reanalise/34510").status_code, 401)

    def test_reserva_inexistente_e_400(self):
        csrf = self.entrar()
        r = self.pedir(99999, csrf)
        self.assertEqual(r.status_code, 400)
        self.assertIn("listagem do CV", r.json()["detail"])
        self.assertEqual(self.pedidos(), [])

    def test_corpo_invalido_e_400(self):
        csrf = self.entrar()
        for corpo in ({"idreserva": "34510"}, {"idreserva": True}, {"idreserva": -1}, {}, [34510]):
            with self.subTest(corpo=corpo):
                r = self.client.post("/api/reanalise", json=corpo, headers={"X-CSRF-Token": csrf})
                self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/reanalise", content=b"{nao-e-json", headers={"X-CSRF-Token": csrf})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.pedidos(), [])

    def test_pedido_grava_quem_pediu_e_duplicado_devolve_o_existente(self):
        csrf = self.entrar()
        r1 = self.pedir(34510, csrf)
        self.assertEqual(r1.status_code, 200, r1.text)
        p1 = r1.json()
        self.assertTrue(p1["novo"])
        self.assertEqual((p1["pedido"]["status"], p1["pedido"]["pedido_por"], p1["pedido"]["origem"]),
                         ("na_fila", "ana", "painel"))
        r2 = self.pedir(34510, csrf)
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["novo"])
        self.assertEqual(r2.json()["pedido"]["id"], p1["pedido"]["id"])
        self.assertEqual(len(self.pedidos()), 1)

    def test_em_andamento_tambem_e_duplicado(self):
        csrf = self.entrar()
        id_ = self.pedir(34510, csrf).json()["pedido"]["id"]
        self.banco.pegar_proximo_pedido()  # o executor pegou
        r = self.pedir(34510, csrf)
        self.assertEqual((r.json()["novo"], r.json()["pedido"]["id"], r.json()["pedido"]["status"]),
                         (False, id_, "em_andamento"))

    def test_teto_de_20(self):
        ids = list(range(40001, 40022))  # 21 reservas
        self.escrever_listagem({str(i): {"idreserva": i, "situacao": "Nova Reserva"} for i in ids})
        csrf = self.entrar()
        for i in ids[:20]:
            self.assertEqual(self.pedir(i, csrf).status_code, 200)
        r = self.pedir(ids[20], csrf)
        self.assertEqual(r.status_code, 429)
        self.assertIn("20", r.json()["detail"])
        # cheia, o pedido repetido de quem já está na fila continua devolvendo o existente
        r = self.pedir(ids[0], csrf)
        self.assertEqual((r.status_code, r.json()["novo"]), (200, False))
        self.assertEqual(len(self.pedidos()), 20)

    def test_listagem_nova_vale_sem_reiniciar_o_site(self):
        csrf = self.entrar()
        self.assertEqual(self.pedir(50001, csrf).status_code, 400)
        self.escrever_listagem({"50001": {"idreserva": 50001, "situacao": "Nova Reserva"}})
        self.assertEqual(self.pedir(50001, csrf).status_code, 200)

    def test_get_sem_pedido(self):
        self.entrar()
        r = self.client.get("/api/reanalise/34510")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"idreserva": 34510, "pedido": None, "posicao": None, "aguardando": None})

    def test_get_mostra_a_espera_pela_varredura(self):
        csrf = self.entrar()
        self.pedir(34520, csrf)
        self.pedir(34510, csrf)
        casos = [("varredura", "aguardando a varredura diária terminar"),
                 ("repescagem", "aguardando a varredura diária terminar"),
                 ("funil", "aguardando a atualização do funil (segundos)"),
                 ("reanalise", "aguardando outra reanálise terminar"),
                 (None, "aguardando o executor de reanálise")]
        for modo, texto in casos:
            with self.subTest(dono=modo):
                self.trava_viva(modo) if modo else vd.ARQ_TRAVA.unlink(missing_ok=True)
                j = self.client.get("/api/reanalise/34510").json()
                self.assertEqual((j["pedido"]["status"], j["posicao"], j["aguardando"]), ("na_fila", 2, texto))

    def test_get_do_concluido_nao_tem_espera(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.executor().atender_um()
        j = self.client.get("/api/reanalise/34510").json()
        self.assertEqual((j["pedido"]["status"], j["posicao"], j["aguardando"]), ("concluido", None, None))
        self.assertIsNotNone(j["pedido"]["concluido_em"])


# ==========================================================================
# Executor
# ==========================================================================
class Executor(BaseFila):
    def test_processa_e_grava_concluido(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        mvp_v2_reserva._LISTAGEM_DE_ASSOCIADOS["velha"] = [{"idreserva": 34510}]
        status = self.executor().atender_um()
        self.assertEqual(status, "concluido")
        p = self.pedidos()[0]
        self.assertEqual((p["status"], p["resultado"], p["erro"]), ("concluido", "2 pasta(s) analisada(s)", None))
        self.assertIsNotNone(p["iniciado_em"])
        self.assertIsNotNone(p["concluido_em"])
        self.assertEqual(self.cv["analisadas"], [34510], "a MESMA _analisar_reserva() da varredura")
        self.assertEqual(mvp_v2_reserva._LISTAGEM_DE_ASSOCIADOS, {}, "cache de associados esvaziado")
        self.assertFalse(vd.ARQ_TRAVA.exists(), "soltou a trava")
        self.assertFalse(vd.ARQ_REGISTRO.exists(), "nada em varreduras.jsonl")
        self.assertFalse(vd.ARQ_ULTIMA_OK.exists(), "nada no marcador do dia")
        logs = list(vd.PASTA_LOGS.glob("reanalise-*.txt"))
        self.assertEqual(len(logs), 1)
        texto = logs[0].read_text(encoding="utf-8")
        self.assertIn("pedido 1: reserva 34510 (pedido por ana, origem painel", texto)
        self.assertIn("pedido 1: concluido - 2 pasta(s) analisada(s)", texto)
        self.assertEqual(list(vd.PASTA_LOGS.glob("varredura-*.txt")), [])

    def test_pega_a_trava_com_modo_reanalise(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        visto = []
        anterior = vd.processar_reserva_completa

        def olhando_a_trava(idr, devolver_associados=False):
            visto.append(vd._ler_trava())
            return anterior(idr, devolver_associados)
        vd.processar_reserva_completa = olhando_a_trava
        self.executor().atender_um()
        self.assertEqual((visto[0]["modo"], visto[0]["pid"]), ("reanalise", os.getpid()))

    def test_um_pedido_por_vez_na_ordem(self):
        csrf = self.entrar()
        for i in (34520, 34510, 34530):
            self.pedir(i, csrf)
        e = self.executor()
        self.assertEqual(e.atender_um(), "concluido")
        self.assertEqual(self.cv["analisadas"], [34520], "um por volta: a folga é da varredura")
        e.atender_um()
        e.atender_um()
        self.assertEqual(self.cv["analisadas"], [34520, 34510, 34530])
        self.assertIsNone(e.atender_um(), "fila vazia")

    def test_trava_ocupada_o_pedido_continua_na_fila(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        for modo in ("varredura", "repescagem", "funil", "reanalise", None):
            with self.subTest(dono=modo):
                trava = self.trava_viva(modo)
                self.assertEqual(self.executor().atender_um(), "trava_ocupada")
                self.assertEqual(self.pedidos()[0]["status"], "na_fila")
                self.assertEqual(self.cv["analisadas"], [])
                self.assertEqual(vd.ARQ_TRAVA.read_bytes(), trava, "a trava dos outros fica")
        vd.ARQ_TRAVA.unlink()
        self.assertEqual(self.executor().atender_um(), "concluido", "trava livre: atende")

    def test_espera_logada_uma_vez_so(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.trava_viva("varredura")
        e = self.executor()
        for _ in range(5):
            e.atender_um()
        texto = next(vd.PASTA_LOGS.glob("reanalise-*.txt")).read_text(encoding="utf-8")
        self.assertEqual(texto.count("esperando"), 1)

    def test_fora_do_funil(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.cv["situacao"][34510] = "Venda Validada"
        self.assertEqual(self.executor().atender_um(), "fora_do_funil")
        p = self.pedidos()[0]
        self.assertEqual((p["status"], p["resultado"]), ("fora_do_funil", "saiu do funil: agora em Venda Validada"))
        self.assertEqual(self.cv["analisadas"], [], "não baixa nem chama a IA")
        listagem = json.loads(vd.ARQ_LISTAGEM.read_text(encoding="utf-8"))
        self.assertEqual(listagem["reservas"]["34510"]["situacao"], "Venda Validada", "corrige a cópia local")
        self.assertEqual(listagem["atualizado_em"], "2026-10-19 12:00:00", "não finge que o funil foi atualizado")

    def test_falha_da_reserva(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.cv["quebrar"].add(34510)
        self.assertEqual(self.executor().atender_um(), "falhou")
        p = self.pedidos()[0]
        self.assertEqual((p["status"], p["erro"]), ("falhou", "Erro 422 ao buscar documentos da reserva 34510"))
        self.assertFalse(vd.ARQ_TRAVA.exists())
        # falhou não é aberto: pode pedir de novo
        self.assertTrue(self.pedir(34510, csrf).json()["novo"])

    def test_em_andamento_antigo_vira_falhou_quando_o_executor_sobe(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.pedir(34520, csrf)
        velho = self.banco.pegar_proximo_pedido()   # executor anterior pegou e morreu
        recente = self.banco.pegar_proximo_pedido()
        con = sqlite3.connect(self.cfg.db)
        con.execute("UPDATE pedidos_reanalise SET iniciado_em = ? WHERE id = ?",
                    (banco_mod.para_texto(datetime.now() - timedelta(minutes=25)), velho["id"]))
        con.execute("UPDATE pedidos_reanalise SET iniciado_em = ? WHERE id = ?",
                    (banco_mod.para_texto(datetime.now() - timedelta(minutes=5)), recente["id"]))
        con.commit()
        con.close()
        # sobe pelo main() de verdade, com --uma-vez
        carregar = ex.config_painel.carregar
        ex.config_painel.carregar = lambda: self.cfg
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ex.main(["--uma-vez"], eco=False), 0)
        finally:
            ex.config_painel.carregar = carregar
        por_id = {p["id"]: p for p in self.pedidos()}
        self.assertEqual((por_id[velho["id"]]["status"], por_id[velho["id"]]["erro"]), ("falhou", "executor reiniciado"))
        self.assertEqual(por_id[recente["id"]]["status"], "em_andamento", "o de 5 min ainda não é órfão")
        self.assertIn("marcados como falhou", next(vd.PASTA_LOGS.glob("reanalise-*.txt")).read_text(encoding="utf-8"))

    def test_concluir_de_quem_ja_virou_orfao_nao_reescreve(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        p = self.banco.pegar_proximo_pedido()
        self.banco.recuperar_pedidos_orfaos(datetime.now() + timedelta(minutes=30))
        self.assertFalse(self.banco.concluir_pedido(p["id"], "concluido", "x"))
        self.assertEqual(self.pedidos()[0]["status"], "falhou")

    def test_varredura_espera_a_reanalise(self):
        self.trava_viva("reanalise")
        vd_intervalo = vd.INTERVALO_DA_ESPERA_S
        vd.INTERVALO_DA_ESPERA_S = 0.1
        try:
            threading.Timer(0.4, lambda: vd.ARQ_TRAVA.unlink(missing_ok=True)).start()
            with contextlib.redirect_stdout(io.StringIO()) as saida:
                token, esperou = vd._pegar_trava_esperando("varredura")
        finally:
            vd.INTERVALO_DA_ESPERA_S = vd_intervalo
        self.assertTrue(token)
        self.assertTrue(esperou)
        self.assertIn("A trava está com o reanalise", saida.getvalue())
        vd._soltar_trava(token)


# ==========================================================================
# Revisão da Forja da Parte B: timeout da IA (P1) e intervalo mínimo (P2)
# ==========================================================================
class CorrecoesForja(BaseFila):
    def test_cliente_da_ia_nasce_com_timeout(self):
        import types as pytypes
        import google
        import analisar_documentos as ad
        criados = []
        falso = pytypes.ModuleType("google.genai")
        falso_types = pytypes.ModuleType("google.genai.types")
        falso_types.HttpOptions = lambda **k: ("HttpOptions", k)
        falso.types = falso_types
        falso.Client = lambda **k: criados.append(k) or "cliente"
        guardados = {n: sys.modules.get(n) for n in ("google.genai", "google.genai.types")}
        attr = getattr(google, "genai", None)
        sys.modules["google.genai"], sys.modules["google.genai.types"] = falso, falso_types
        google.genai = falso
        try:
            self.assertEqual(ad.criar_cliente_ia("chave-falsa"), "cliente")
        finally:
            for n, m in guardados.items():
                sys.modules[n] = m
            google.genai = attr
        self.assertEqual(criados, [{"api_key": "chave-falsa",
                                    "http_options": ("HttpOptions", {"timeout": ad.TIMEOUT_IA_MS})}])
        self.assertEqual(ad.TIMEOUT_IA_MS, 120_000)

    def test_timeout_da_ia_falha_o_documento_e_o_pedido_termina(self):
        import analisar_documentos as ad
        pasta = self.tmp / "pasta-ia"
        pasta.mkdir()
        (pasta / "rg.png").write_bytes(b"\x89PNG ficticio")
        resultados = []

        def ia_pendurada(*a, **k):
            raise TimeoutError("The read operation timed out")

        def pasta_com_um_documento(pasta_cv, **k):
            resultados.append(ad._processar_um_documento(
                None, "modelo", pasta, {"arquivo_local": "rg.png", "tipo": "RG, CPF, CNH ou outros"},
                {"nome": "FICTICIA"}, 1, 1, threading.Lock()))
        original = ad.extrair_dados_do_documento
        ad.extrair_dados_do_documento = ia_pendurada
        vd.processar_pasta_cliente = pasta_com_um_documento
        try:
            csrf = self.entrar()
            self.pedir(34510, csrf)
            with contextlib.redirect_stdout(io.StringIO()):
                status = self.executor().atender_um()
        finally:
            ad.extrair_dados_do_documento = original
        self.assertEqual(status, "concluido", "a reserva não cai por um documento")
        # 21/10/2026: falha da IA deixa o documento aguardando leitura, sem divergência.
        self.assertEqual(resultados[0]["falha_ia"]["classe"], "rede_timeout_5xx")
        self.assertEqual((resultados[0]["divergencias"], resultados[0]["avisos"]), ([], []))
        self.assertFalse(vd.ARQ_TRAVA.exists(), "trava solta")
        self.assertEqual(self.pedidos()[0]["status"], "concluido")

    def test_intervalo_de_5_min_depois_de_concluida(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.executor().atender_um()
        r = self.pedir(34510, csrf)
        self.assertEqual(r.status_code, 429)
        concluido = banco_mod.de_texto(self.pedidos()[0]["concluido_em"])
        self.assertEqual(r.json()["detail"], f"reanalisada às {concluido:%H:%M}, tente de novo depois das "
                                             f"{concluido + timedelta(minutes=5):%H:%M}")
        self.assertEqual(len(self.pedidos()), 1)
        self.assertEqual(self.pedir(34520, csrf).status_code, 200, "só a mesma reserva espera")
        con = sqlite3.connect(self.cfg.db)
        con.execute("UPDATE pedidos_reanalise SET concluido_em = ? WHERE idreserva = 34510",
                    (banco_mod.para_texto(datetime.now() - timedelta(minutes=6)),))
        con.commit()
        con.close()
        self.assertTrue(self.pedir(34510, csrf).json()["novo"], "passados 5 min, pode")

    def test_intervalo_e_por_origem(self):
        self.assertEqual(banco_mod.INTERVALO_MINIMO_POR_ORIGEM,
                         {"painel": timedelta(minutes=5), "webhook": None})
        self.banco.pedir_reanalise(34510, "painel", "ana", {34510})
        self.executor().atender_um()
        with self.assertRaises(banco_mod.ReanaliseRecente):
            self.banco.pedir_reanalise(34510, "painel", "ana", {34510})
        _, novo = self.banco.pedir_reanalise(34510, "webhook", "cv", {34510})
        self.assertTrue(novo, "webhook ainda sem intervalo próprio")

    def test_falhou_e_fora_do_funil_nao_contam_para_o_intervalo(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.cv["situacao"][34510] = "Venda Validada"
        self.executor().atender_um()
        # o executor gravou "Venda Validada" na listagem; ela volta ao funil
        self.escrever_listagem({"34510": {"idreserva": 34510, "situacao": "Nova Reserva"}})
        self.assertTrue(self.pedir(34510, csrf).json()["novo"])

    def test_post_recusa_reserva_fora_do_funil(self):
        self.escrever_listagem({"34510": {"idreserva": 34510, "situacao": "Venda Validada"},
                                "34520": {"idreserva": 34520, "situacao": " Nova Reserva "}})
        csrf = self.entrar()
        r = self.pedir(34510, csrf)
        self.assertEqual((r.status_code, r.json()["detail"]),
                         (400, "reserva fora do funil segundo a listagem do CV"))
        self.assertEqual(self.pedidos(), [])
        self.assertEqual(self.pedir(34520, csrf).status_code, 200, "espaços nas pontas não tiram do funil")


# ==========================================================================
# Banco e instância única
# ==========================================================================
class BancoEInstancia(BaseFila):
    def test_um_aberto_por_reserva_garantido_pelo_banco(self):
        con = sqlite3.connect(self.cfg.db)
        insert = ("INSERT INTO pedidos_reanalise (idreserva, origem, pedido_por, pedido_em, status) "
                  "VALUES (34510, 'painel', 'ana', '2026-10-19 12:00:00', ?)")
        con.execute(insert, ("na_fila",))
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(insert, ("em_andamento",))
        con.execute(insert, ("concluido",))  # fechado não conta
        con.close()

    def test_tabela_nasce_em_banco_que_ja_existe_sem_perder_nada(self):
        # banco de antes da Parte B: sem a tabela, com dados
        auth_antes = self.banco.buscar_usuario("ana")
        con = sqlite3.connect(self.cfg.db)
        con.execute("DROP TABLE pedidos_reanalise")
        con.commit()
        con.close()
        novo = banco_mod.Banco(self.cfg.db)
        self.assertEqual(novo.buscar_usuario("ana"), auth_antes)
        self.assertEqual(novo.ultimo_pedido(34510), None)
        banco_mod.Banco(self.cfg.db)  # de novo: idempotente
        self.assertEqual(self.pedidos(), [])

    def test_pedidos_simultaneos_de_dois_bancos_geram_um_so(self):
        """Duas conexões independentes (como o painel e o executor, que são
        processos diferentes), 8 threads cada, pedindo a MESMA reserva e
        enchendo a fila ao mesmo tempo: um pedido por reserva e nunca mais que
        o teto."""
        bancos = [self.banco, banco_mod.Banco(self.cfg.db)]
        ids = list(range(60001, 60031))  # 30 reservas para um teto de 20
        validas = set(ids) | {34510}
        erros, barreira = [], threading.Barrier(16)

        def pedir_varias(banco, deslocamento):
            barreira.wait()
            for i in [34510] + ids[deslocamento::2]:
                try:
                    banco.pedir_reanalise(i, "painel", "ana", validas)
                except banco_mod.FilaCheia:
                    pass
                except Exception as exc:  # noqa: BLE001
                    erros.append(exc)
        threads = [threading.Thread(target=pedir_varias, args=(bancos[n % 2], n % 2)) for n in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(erros, [])
        pedidos = self.pedidos()
        self.assertEqual(sum(1 for p in pedidos if p["idreserva"] == 34510), 1)
        self.assertEqual(len(pedidos), 20, "teto respeitado")
        self.assertEqual(len({p["idreserva"] for p in pedidos}), 20, "sem repetição")

    def test_origem_e_autor_validados_no_banco(self):
        validas = {34510}
        with self.assertRaises(ValueError):
            self.banco.pedir_reanalise(34510, "email", "ana", validas)
        with self.assertRaises(ValueError):
            self.banco.pedir_reanalise(34510, "painel", "  ", validas)
        with self.assertRaises(banco_mod.ReservaInexistente):
            self.banco.pedir_reanalise(1, "webhook", "cv", validas)
        pedido, novo = self.banco.pedir_reanalise(34510, "webhook", "cv", validas)
        self.assertEqual((pedido["origem"], novo), ("webhook", True))

    def test_so_um_executor_por_vez(self):
        with ex.instancia_unica("SecretariaTeste-" + str(os.getpid())) as primeiro:
            with ex.instancia_unica("SecretariaTeste-" + str(os.getpid())) as segundo:
                self.assertEqual((primeiro, segundo), (True, False))
        with ex.instancia_unica("SecretariaTeste-" + str(os.getpid())) as de_novo:
            self.assertTrue(de_novo, "solto quando o primeiro sai")

    def test_main_sai_se_ja_ha_executor(self):
        with ex.instancia_unica() as primeiro:
            self.assertTrue(primeiro)
            with contextlib.redirect_stdout(io.StringIO()) as saida:
                self.assertEqual(ex.main(["--uma-vez"], eco=False), 0)
        self.assertIn("Já há um executor", saida.getvalue())


if __name__ == "__main__":
    unittest.main()
