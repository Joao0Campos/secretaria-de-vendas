"""Testes do "Buscar reservas novas" (Parte C2, 19/10/2026) - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_busca_funil.py -v

O pedido de tipo 'funil' vai para a mesma fila; o executor atualiza a listagem
(atualizar_listagem simulada) e enfileira "Analisar" das reservas do funil sem
planilha. Mesma blindagem e templates de verdade do test_telas_reanalise.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import test_telas_reanalise as tt  # noqa: E402  (blindagem antes do projeto)

import sqlite3  # noqa: E402
import threading  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import executor_reanalise as ex  # noqa: E402
import varredura_diaria as vd  # noqa: E402
from painel import banco as banco_mod  # noqa: E402

tr = tt.tr


class BaseBusca(tt.BaseTelas):
    def setUp(self):
        super().setUp()
        self.depois = {1001: "Nova Reserva", 34510: "Nova Reserva", 34520: "Reserva Aprovada",
                       34530: "Análise Gerencial", 99: "Venda Validada"}
        self.cv_listagem = {"chamadas": 0, "quebrar": False}

        def listagem_do_cv(cfg):
            self.cv_listagem["chamadas"] += 1
            if self.cv_listagem["quebrar"]:
                raise SystemExit("Erro 500 na listagem incremental (página 1): corpo cru")
            self.listagem(self.depois, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return vd._ler_listagem()["reservas"], "incremental (teste)"
        vd.atualizar_listagem = listagem_do_cv
        self.listagem({1001: "Nova Reserva"})  # antes da busca: nada novo

    def tearDown(self):
        vd.atualizar_listagem = ORIGINAL_ATUALIZAR_LISTAGEM
        super().tearDown()

    def executor(self):
        return ex.Executor(self.banco, eco=False, saidas=self.cfg.saidas)

    def buscar(self, csrf):
        return self.client.post("/api/busca-funil", json={}, headers={"X-CSRF-Token": csrf})

    def estado(self):
        r = self.client.get("/api/busca-funil")
        self.assertEqual(r.status_code, 200)
        return r.json()


ORIGINAL_ATUALIZAR_LISTAGEM = vd.atualizar_listagem


class Rotas(BaseBusca):
    def test_sem_csrf_403_e_sem_login_401(self):
        self.assertEqual(self.client.post("/api/busca-funil", json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/busca-funil").status_code, 401)
        self.entrar()
        self.assertEqual(self.client.post("/api/busca-funil", json={}).status_code, 403)
        self.assertEqual(self.pedidos(), [])

    def test_pedido_de_funil_e_busca_aberta_nao_duplica(self):
        csrf = self.entrar()
        r = self.buscar(csrf)
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertTrue(j["novo"])
        self.assertEqual((j["fase"], j["pedido"]["tipo"], j["pedido"]["idreserva"], j["pedido"]["pedido_por"]),
                         ("na_fila", "funil", 0, "ana"))
        r2 = self.buscar(csrf)
        self.assertEqual((r2.status_code, r2.json()["novo"], r2.json()["pedido"]["id"]),
                         (200, False, j["pedido"]["id"]))
        self.assertEqual(len(self.pedidos()), 1)

    def test_intervalo_de_2_min_entre_buscas(self):
        csrf = self.entrar()
        self.buscar(csrf)
        self.executor().atender_um()
        r = self.buscar(csrf)
        self.assertEqual(r.status_code, 429)
        pedido_em = banco_mod.de_texto(self.pedidos()[0]["pedido_em"])
        self.assertEqual(r.json()["detail"], f"busca feita às {pedido_em:%H:%M}, tente de novo depois das "
                                             f"{pedido_em + timedelta(minutes=2):%H:%M}")
        con = sqlite3.connect(self.cfg.db)
        con.execute("UPDATE pedidos_reanalise SET pedido_em = ? WHERE tipo = 'funil'",
                    (banco_mod.para_texto(datetime.now() - timedelta(minutes=3)),))
        con.commit()
        con.close()
        self.assertTrue(self.buscar(csrf).json()["novo"])

    def test_busca_nao_aparece_como_reanalise_nem_conta_no_teto(self):
        csrf = self.entrar()
        self.buscar(csrf)
        self.assertIsNone(self.client.get("/api/reanalise/0").json()["pedido"])
        ids = list(range(40001, 40021))
        self.listagem({i: "Nova Reserva" for i in ids})
        for i in ids:  # 20 reanálises cabem com a busca na fila
            self.assertEqual(self.pedir(i, csrf).status_code, 200)

    def test_espera_a_varredura(self):
        csrf = self.entrar()
        self.buscar(csrf)
        self.trava_viva("varredura")
        self.assertEqual(self.executor().atender_um(), "trava_ocupada")
        e = self.estado()
        self.assertEqual((e["fase"], e["aguardando"]), ("na_fila", "aguardando a varredura diária terminar"))
        self.assertEqual(self.cv_listagem["chamadas"], 0)


class Executor(BaseBusca):
    def test_atualiza_o_funil_e_enfileira_as_sem_planilha(self):
        csrf = self.entrar()
        self.buscar(csrf)
        e = self.executor()
        self.assertEqual(e.atender_um(), "concluido")
        self.assertEqual(self.cv_listagem["chamadas"], 1, "o mesmo miolo do --atualizar-funil")
        self.assertFalse(vd.ARQ_TRAVA.exists())
        reanalises = [p for p in self.pedidos() if p["tipo"] == "reserva"]
        self.assertEqual([(p["idreserva"], p["origem"], p["pedido_por"], p["status"]) for p in reanalises],
                         [(34530, "painel", "ana", "na_fila"), (34520, "painel", "ana", "na_fila"),
                          (34510, "painel", "ana", "na_fila")], "1001 tem planilha e 99 está fora do funil")
        est = self.estado()
        self.assertEqual((est["fase"], est["total"], est["analisadas"], est["abertas"], est["fora_pelo_teto"]),
                         ("analisando", 3, 0, 3, 0))
        for _ in range(3):
            e.atender_um()
        est = self.estado()
        self.assertEqual((est["fase"], est["total"], est["analisadas"], est["abertas"]), ("concluida", 3, 3, 0))
        self.assertEqual(sorted(self.cv["analisadas"]), [34510, 34520, 34530])
        texto = next(vd.PASTA_LOGS.glob("reanalise-*.txt")).read_text(encoding="utf-8")
        self.assertIn("busca: 3 sem análise no funil; 3 na fila, 0 já estavam, 0 de fora pelo teto", texto)
        self.assertFalse(vd.ARQ_REGISTRO.exists(), "nada em varreduras.jsonl")

    def test_falha_contada_no_progresso(self):
        csrf = self.entrar()
        self.buscar(csrf)
        self.cv["quebrar"].add(34520)
        e = self.executor()
        for _ in range(4):
            e.atender_um()
        est = self.estado()
        self.assertEqual((est["fase"], est["analisadas"], est["falharam"]), ("concluida", 2, 1))

    def test_teto_deixa_de_fora_e_avisa(self):
        self.depois = {i: "Nova Reserva" for i in range(50001, 50023)}  # 22 sem planilha
        csrf = self.entrar()
        self.buscar(csrf)
        self.executor().atender_um()
        est = self.estado()
        self.assertEqual((est["total"], est["fora_pelo_teto"]), (20, 2))
        self.assertEqual(sum(1 for p in self.pedidos() if p["tipo"] == "reserva"), 20)

    def test_nao_duplica_o_que_ja_estava_na_fila(self):
        self.listagem({1001: "Nova Reserva", 34510: "Nova Reserva"})
        csrf = self.entrar()
        self.pedir(34510, csrf)
        self.buscar(csrf)
        e = self.executor()
        e.atender_um()  # 34510 (primeiro da fila)
        e.atender_um()  # a busca
        reanalises = [p["idreserva"] for p in self.pedidos() if p["tipo"] == "reserva"]
        self.assertEqual(sorted(reanalises), [34510, 34520, 34530], "34510 já tinha planilha? não: 1 pedido só")
        self.assertEqual(reanalises.count(34510), 1)

    def test_falha_do_funil(self):
        self.cv_listagem["quebrar"] = True
        csrf = self.entrar()
        self.buscar(csrf)
        self.assertEqual(self.executor().atender_um(), "falhou")
        est = self.estado()
        self.assertEqual((est["fase"], est["pedido"]["erro"]), ("falhou", "Erro 500 na listagem incremental (página 1)"))
        self.assertEqual([p for p in self.pedidos() if p["tipo"] == "reserva"], [])
        self.assertFalse(vd.ARQ_TRAVA.exists())


class TelaEBanco(BaseBusca):
    def test_quadro_na_lista(self):
        self.entrar()
        pagina = self.html("/")
        self.assertIn("data-busca", pagina)
        self.assertIn("Buscar reservas novas", pagina)
        js = self.client.get("/static/painel.js").text
        for trecho in ('"/api/busca-funil"', "5000", "de fora (fila cheia, máx. 20)", "Atualizando o funil..."):
            self.assertIn(trecho, js)

    def test_banco_antigo_ganha_a_coluna_tipo(self):
        self.listagem({1001: "Nova Reserva", 34510: "Nova Reserva"})
        csrf = self.entrar()
        self.assertEqual(self.pedir(34510, csrf).status_code, 200)
        con = sqlite3.connect(self.cfg.db)
        con.executescript("""
            CREATE TABLE velha (id INTEGER PRIMARY KEY AUTOINCREMENT, idreserva INTEGER NOT NULL,
                origem TEXT NOT NULL, pedido_por TEXT NOT NULL, pedido_em TEXT NOT NULL, status TEXT NOT NULL,
                iniciado_em TEXT, concluido_em TEXT, resultado TEXT, erro TEXT);
            INSERT INTO velha SELECT id, idreserva, origem, pedido_por, pedido_em, status,
                iniciado_em, concluido_em, resultado, erro FROM pedidos_reanalise;
            DROP TABLE pedidos_reanalise;
            ALTER TABLE velha RENAME TO pedidos_reanalise;""")
        con.close()
        novo = banco_mod.Banco(self.cfg.db)
        self.assertEqual(novo.ultimo_pedido(34510)["tipo"], "reserva")
        pedido, _ = novo.pedir_busca_do_funil("painel", "ana")
        self.assertEqual(pedido["tipo"], "funil")

    def test_migracao_com_painel_e_executor_subindo_juntos(self):
        """P2 da Forja: a coluna acrescentada por outro processo entre o PRAGMA
        e o ALTER ('duplicate column') conta como já migrado; e vários Banco()
        subindo juntos num banco antigo não falham."""
        con = sqlite3.connect(self.cfg.db)
        con.row_factory = sqlite3.Row

        class PragmaAtrasado:  # viu o banco antes do outro processo acrescentar
            def execute(self, sql, *a):
                linhas = con.execute(sql, *a)
                if sql.startswith("PRAGMA table_info"):
                    return [l for l in linhas if l["name"] not in ("tipo", "detalhe")]
                return linhas
        banco_mod.Banco._migrar(PragmaAtrasado())
        con.close()

        for rodada in range(3):
            caminho = self.cfg.db.with_name(f"antigo{rodada}.db")
            banco_mod.Banco(caminho)
            velho = sqlite3.connect(caminho)
            velho.executescript("""
                CREATE TABLE velha (id INTEGER PRIMARY KEY AUTOINCREMENT, idreserva INTEGER NOT NULL,
                    origem TEXT NOT NULL, pedido_por TEXT NOT NULL, pedido_em TEXT NOT NULL, status TEXT NOT NULL,
                    iniciado_em TEXT, concluido_em TEXT, resultado TEXT, erro TEXT);
                DROP TABLE pedidos_reanalise;
                ALTER TABLE velha RENAME TO pedidos_reanalise;""")
            velho.close()
            largada, erros = threading.Barrier(6), []

            def subir():
                largada.wait()
                try:
                    banco_mod.Banco(caminho)
                except Exception as exc:  # noqa: BLE001
                    erros.append(exc)
            fios = [threading.Thread(target=subir) for _ in range(6)]
            [t.start() for t in fios]
            [t.join() for t in fios]
            self.assertEqual(erros, [])
            olhar = sqlite3.connect(caminho)
            colunas = [l[1] for l in olhar.execute("PRAGMA table_info(pedidos_reanalise)")]
            olhar.close()
            self.assertEqual(colunas.count("tipo"), 1)


if __name__ == "__main__":
    unittest.main()
