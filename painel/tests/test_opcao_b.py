"""Opção B (20/10/2026): só o site no servidor; o PC envia os arquivos e o
executor usa a fila pela API, com a chave - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_opcao_b.py -v

O cliente HTTP de verdade (cliente_servidor.Cliente) fala com o site de teste
por uma sessão adaptada ao TestClient: nenhuma rede. O "PC" e o "servidor" são
pastas temporárias diferentes.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import test_reanalise as tr  # noqa: E402  (blindagem antes do projeto)

import dataclasses  # noqa: E402
import stat as stat_mod  # noqa: E402
import types  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

import requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import cliente_servidor as cs  # noqa: E402
import executor_reanalise as ex  # noqa: E402
import sincronizar_servidor as ss  # noqa: E402
from painel import backup as backup_mod  # noqa: E402
from painel import congelar as congelar_mod  # noqa: E402

PROCESSOS_REAIS = congelar_mod._processos_do_projeto
from painel import banco as banco_mod  # noqa: E402
from painel import sincronia  # noqa: E402
from painel.app import criar_app  # noqa: E402

tp = tr.tp
CHAVE = "chave-de-teste-do-executor-0123456789"
HASH = hashlib.sha256(CHAVE.encode()).hexdigest()


class SessaoDeTeste:
    """Faz o papel de requests.Session para o Cliente de verdade, sobre o TestClient."""

    def __init__(self, client, cair_depois_de: int | None = None):
        self.client, self.cair_depois_de, self.chamadas = client, cair_depois_de, []

    def request(self, metodo, url, headers=None, timeout=None, verify=None, json=None, data=None, params=None,
                stream=False):
        if self.cair_depois_de is not None and len(self.chamadas) >= self.cair_depois_de:
            raise requests.ConnectionError("rede caiu")
        self.chamadas.append((metodo, url.split("testserver", 1)[-1]))
        if hasattr(data, "read"):
            data = data.read()
        resp = self.client.request(metodo, url, headers=headers, json=json, content=data, params=params)
        return _EmPedacos(resp, self.estragar) if stream else resp

    estragar = False  # corrompe o corpo baixado (sha divergente)


class _EmPedacos:
    """O mínimo de requests.Response em streaming (iter_content), sobre o httpx do TestClient."""

    def __init__(self, resp, estragar):
        self.status_code, self.headers, self._resp, self._estragar = resp.status_code, resp.headers, resp, estragar

    def iter_content(self, tamanho):
        for pedaco in self._resp.iter_bytes(tamanho):
            yield (b"X" + pedaco[1:]) if self._estragar and pedaco else pedaco

    def close(self):
        self._resp.close()


class BaseB(tr.BaseFila):
    def setUp(self):
        super().setUp()
        self.servidor_saidas = self.tmp / "servidor_saidas"
        self.servidor_saidas.mkdir()
        self.client.close()
        self.cfg = dataclasses.replace(self.cfg, executor_chave_hash=HASH, saidas=self.servidor_saidas)
        self.app = criar_app(self.cfg, templates_dir=tp.TEMPLATES_FAKE)
        self.banco = self.app.state.banco
        self.client = TestClient(self.app, follow_redirects=False)

    def cliente(self, cair_depois_de=None, chave=CHAVE) -> cs.Cliente:
        return cs.Cliente("https://testserver", chave, sessao=SessaoDeTeste(self.client, cair_depois_de))

    def api(self, rota, dados=None, chave=CHAVE):
        cab = {"Authorization": f"Bearer {chave}"} if chave is not None else {}
        return self.client.post(rota, json=dados or {}, headers=cab)


# ==========================================================================
# Rotas do executor: chave, atomicidade, estado
# ==========================================================================
class RotasDoExecutor(BaseB):
    ROTAS = ("/api/executor/estado", "/api/executor/proximo", "/api/executor/concluir",
             "/api/executor/enfileirar", "/api/sync/manifesto")

    def test_chave_errada_ou_ausente_e_401(self):
        self.banco.pedir_reanalise(34510, "painel", "ana", tr.RESERVAS)
        for rota in self.ROTAS:
            for chave, cab in ((None, {}), ("errada", None), (CHAVE, "Basic")):
                with self.subTest(rota=rota, chave=chave, cab=cab):
                    if cab == "Basic":
                        r = self.client.post(rota, json={}, headers={"Authorization": f"Basic {CHAVE}"})
                    else:
                        r = self.api(rota, chave=chave)
                    self.assertEqual(r.status_code, 401)
        r = self.client.put("/api/sync/arquivo", params={"caminho": "painel_dados/varreduras.jsonl", "tamanho": 1,
                                                         "mtime_ns": 0, "sha256": "0" * 64},
                            content=b"x", headers={"Authorization": "Bearer errada"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.pedidos()[0]["status"], "na_fila", "nada foi entregue")
        self.assertFalse((self.cfg.db.parent / "varreduras.jsonl").exists(), "nada gravado")

    def test_sem_hash_no_servidor_as_rotas_nao_existem(self):
        self.client.close()
        app = criar_app(dataclasses.replace(self.cfg, executor_chave_hash=None), templates_dir=tp.TEMPLATES_FAKE)
        self.client = TestClient(app, follow_redirects=False)
        for rota in self.ROTAS:
            self.assertEqual(self.api(rota).status_code, 404, rota)

    def test_as_rotas_nao_aceitam_sessao_no_lugar_da_chave(self):
        self.entrar()  # usuário logado, sem a chave
        self.assertEqual(self.client.post("/api/executor/proximo", json={}).status_code, 401)

    def test_pedido_nao_e_entregue_duas_vezes(self):
        for idr in tr.RESERVAS:
            self.banco.pedir_reanalise(idr, "painel", "ana", tr.RESERVAS)
        entregues, erros = [], []

        def executor_paralelo():
            cliente = TestClient(self.app)
            try:
                while True:
                    r = cliente.post("/api/executor/proximo", json={}, headers={"Authorization": f"Bearer {CHAVE}"})
                    pedido = r.json()["pedido"]
                    if pedido is None:
                        return
                    entregues.append(pedido["id"])
            except Exception as exc:  # noqa: BLE001
                erros.append(exc)
            finally:
                cliente.close()
        fios = [threading.Thread(target=executor_paralelo) for _ in range(6)]
        [f.start() for f in fios]
        [f.join() for f in fios]
        self.assertEqual(erros, [])
        self.assertEqual(sorted(entregues), sorted(p["id"] for p in self.pedidos()))
        self.assertEqual(len(entregues), len(set(entregues)), "cada pedido para um executor só")

    def test_concluir_uma_vez_so(self):
        self.banco.pedir_reanalise(34510, "painel", "ana", tr.RESERVAS)
        pedido = self.api("/api/executor/proximo").json()["pedido"]
        dados = {"id": pedido["id"], "status": "concluido", "resultado": "2 pasta(s) analisada(s)"}
        self.assertEqual(self.api("/api/executor/concluir", dados).status_code, 200)
        self.assertEqual(self.api("/api/executor/concluir", dados).status_code, 409)
        self.assertEqual(self.api("/api/executor/concluir", {"id": pedido["id"], "status": "xyz"}).status_code, 400)
        self.assertEqual(self.pedidos()[0]["status"], "concluido")

    def test_estado_mostra_o_modo_e_executor_desligado(self):
        csrf = self.entrar()
        self.pedir(34510, csrf)
        aguardando = lambda: self.client.get("/api/reanalise/34510").json()["aguardando"]  # noqa: E731
        self.assertEqual(aguardando(), "executor desligado", "nunca chegou sinal")
        r = self.api("/api/executor/estado", {"modo": "varredura"})
        self.assertEqual(r.json()["ha_pedido"], True)
        self.assertEqual(aguardando(), "aguardando a varredura diária terminar")
        self.api("/api/executor/estado", {"modo": None})
        self.assertEqual(aguardando(), "aguardando o executor de reanálise")
        con = sqlite3.connect(self.cfg.db)
        con.execute("UPDATE executor_estado SET visto_em = ?",
                    (banco_mod.para_texto(datetime.now() - timedelta(minutes=2)),))
        con.commit()
        con.close()
        self.assertTrue(aguardando().startswith("executor desligado (último sinal às"))

    def test_enfileirar_da_busca_confere_a_listagem_do_servidor(self):
        busca, _ = self.banco.pedir_busca_do_funil("painel", "ana")
        r = self.api("/api/executor/enfileirar", {"pedido_id": busca["id"], "ids": [34530]})
        self.assertEqual(r.status_code, 409, "a busca ainda não está em andamento")
        self.assertEqual(self.api("/api/executor/proximo").json()["pedido"]["id"], busca["id"])
        r = self.api("/api/executor/enfileirar", {"pedido_id": busca["id"], "ids": [34530, 99999]})
        self.assertEqual(r.json()["enfileiradas"], [34530])
        self.assertEqual(r.json()["recusadas"], [99999], "fora da listagem do servidor")
        nova = [p for p in self.pedidos() if p["idreserva"] == 34530][0]
        self.assertEqual((nova["origem"], nova["pedido_por"]), ("painel", "ana"))


# ==========================================================================
# Upload: segurança
# ==========================================================================
class UploadSeguro(BaseB):
    def enviar(self, caminho, conteudo=b"conteudo", tamanho=None, sha=None, mtime_ns=1_700_000_000_000_000_000,
               chave=CHAVE):
        return self.client.put("/api/sync/arquivo", content=conteudo, headers={"Authorization": f"Bearer {chave}"},
                               params={"caminho": caminho, "tamanho": len(conteudo) if tamanho is None else tamanho,
                                       "mtime_ns": mtime_ns, "sha256": sha or hashlib.sha256(conteudo).hexdigest()})

    def arquivos_do_servidor(self):
        return sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*")
                      if p.is_file() and ("servidor_saidas" in p.parts or p.parent == self.cfg.db.parent))

    def test_grava_atomico_com_mtime_e_sha(self):
        r = self.enviar("saidas/reserva/FULANA-reserva-1/rg.pdf", b"%PDF documento")
        self.assertEqual(r.status_code, 200, r.text)
        alvo = self.servidor_saidas / "reserva" / "FULANA-reserva-1" / "rg.pdf"
        self.assertEqual(alvo.read_bytes(), b"%PDF documento")
        self.assertEqual(alvo.stat().st_mtime_ns, 1_700_000_000_000_000_000)
        self.assertEqual(self.enviar("painel_dados/reservas_cv.json", b"{}").status_code, 200)
        self.assertEqual(list(self.tmp.rglob("*.parcial")), [])

    def test_path_traversal_e_caminhos_proibidos(self):
        antes = self.arquivos_do_servidor()
        ruins = ["../fora.txt", "saidas/reserva/../../painel_dados/painel.db", "saidas/reserva/x/../../../y.pdf",
                 "/etc/passwd", "C:/Windows/x.txt", "saidas\\reserva\\a\\b.pdf", "saidas/reserva/a\\..\\b.pdf",
                 "painel_dados/painel.db", "painel_dados/../painel_dados/painel.db", "saidas/reserva/a/b/c.pdf",
                 "saidas/reserva/a.pdf", "saidas/pre_cadastro/a/b.pdf", "saidas/reserva/a/nul.txt",
                 "saidas/reserva/a/arq.pdf:fluxo", "saidas/reserva/a/arq.pdf.parcial", "saidas/reserva/a /b.pdf",
                 "saidas/reserva/./b.pdf", "saidas/reserva//b.pdf", "saidas/reserva/a/b\x00.pdf", "", "x" * 500]
        for caminho in ruins:
            with self.subTest(caminho=caminho):
                self.assertEqual(self.enviar(caminho).status_code, 400)
                with self.assertRaises(sincronia.CaminhoInvalido):
                    sincronia.destino(caminho, self.servidor_saidas, self.cfg.db.parent)
        self.assertEqual(self.arquivos_do_servidor(), antes, "nada gravado")
        self.assertTrue(self.cfg.db.exists(), "o banco intacto")

    def test_arquivo_grande(self):
        with mock.patch.object(sincronia, "LIMITE_ARQUIVO", 1000):
            r = self.enviar("saidas/reserva/a/grande.pdf", b"x" * 2000)
            self.assertIn(r.status_code, (413, 422))
            r = self.enviar("saidas/reserva/a/grande.pdf", b"x" * 10, tamanho=2000)
            self.assertEqual(r.status_code, 413, "tamanho declarado acima do limite")
        self.assertFalse((self.servidor_saidas / "reserva" / "a" / "grande.pdf").exists())
        self.assertEqual(list(self.tmp.rglob("*.parcial")), [])

    def test_sha_divergente_e_corpo_diferente_do_declarado(self):
        alvo = self.servidor_saidas / "reserva" / "a" / "doc.pdf"
        self.assertEqual(self.enviar("saidas/reserva/a/doc.pdf", b"versao 1").status_code, 200)
        r = self.enviar("saidas/reserva/a/doc.pdf", b"versao 2", sha=hashlib.sha256(b"outra").hexdigest())
        self.assertEqual(r.status_code, 422)
        self.assertIn("sha256", r.json()["detail"])
        self.assertEqual(self.enviar("saidas/reserva/a/doc.pdf", b"versao 2", tamanho=4).status_code, 413,
                         "corpo maior que o declarado para na hora")
        self.assertEqual(self.enviar("saidas/reserva/a/doc.pdf", b"v2", tamanho=20).status_code, 422)
        self.assertEqual(self.enviar("saidas/reserva/a/doc.pdf", b"v2", sha="nao-e-sha").status_code, 422)
        self.assertEqual(alvo.read_bytes(), b"versao 1", "a versão boa continua lá")
        self.assertEqual(list(self.tmp.rglob("*.parcial")), [])

    def test_manifesto_so_calcula_e_concluir_apaga_por_ultimo(self):
        for c in ("saidas/reserva/a/1.pdf", "saidas/reserva/a/2.pdf", "saidas/reserva/b/3.pdf",
                  "painel_dados/varreduras.jsonl"):
            self.enviar(c, c.encode())
        item = lambda c: {"caminho": c, "tamanho": len(c), "mtime_ns": 1_700_000_000_000_000_000}  # noqa: E731
        tres = self.servidor_saidas / "reserva" / "b" / "3.pdf"
        r = self.api("/api/sync/manifesto", {"arquivos": [item("saidas/reserva/a/1.pdf"),
                                                           dict(item("saidas/reserva/a/2.pdf"), tamanho=99),
                                                           item("saidas/reserva/c/novo.pdf"),
                                                           item("../x")], "apagar": True,
                                             "completo": True}).json()
        self.assertEqual(r["faltam"], ["saidas/reserva/a/2.pdf", "saidas/reserva/c/novo.pdf"])
        self.assertEqual(r["invalidos"], ["../x"])
        self.assertEqual(r["sobrando"], ["saidas/reserva/b/3.pdf"])
        self.assertNotIn("apagados", r)
        self.assertTrue(tres.exists(), "o manifesto só calcula: nada é apagado antes dos envios")
        # concluir com arquivos ainda faltando: recusa
        r = self.api("/api/sync/concluir", {"arquivos": [item("saidas/reserva/a/1.pdf"),
                                                          item("saidas/reserva/c/novo.pdf")],
                                            "completo": True}).json()
        self.assertEqual(r["apagados"], [])
        self.assertIn("faltam", r["recusado"])
        r = self.api("/api/sync/concluir", {"arquivos": [item("saidas/reserva/a/1.pdf")], "completo": False}).json()
        self.assertEqual((r["apagados"], r["recusado"]), ([], "manifesto incompleto"))
        self.assertTrue(tres.exists())
        # o servidor tem tudo o que o manifesto diz e ele é completo: agora sim
        completo = [item("saidas/reserva/a/1.pdf"), item("saidas/reserva/a/2.pdf")]
        r = self.api("/api/sync/concluir", {"arquivos": completo, "completo": True}).json()
        self.assertEqual(r["apagados"], ["saidas/reserva/b/3.pdf"])
        self.assertFalse((self.servidor_saidas / "reserva" / "b").exists(), "pasta vazia sai")
        self.assertTrue((self.cfg.db.parent / "varreduras.jsonl").exists(), "painel_dados nunca é apagado")
        r = self.api("/api/sync/concluir", {"arquivos": [], "completo": True}).json()
        self.assertEqual(r["apagados"], [], "manifesto sem saidas não apaga nada")
        self.assertTrue((self.servidor_saidas / "reserva" / "a" / "1.pdf").exists())
        self.assertEqual(self.api("/api/sync/concluir", {"arquivos": []}, chave="errada").status_code, 401)

    def test_teto_rigido_sem_content_length(self):
        # P2 da Turing: corpo em pedaços (chunked, sem Content-Length) para no teto com 413.
        def pedacos(total, tamanho=8192):
            for _ in range(total // tamanho):
                yield b"x" * tamanho
        cab = {"Authorization": f"Bearer {CHAVE}"}
        r = self.client.post("/api/executor/estado", content=pedacos(256 * 1024), headers=cab)
        self.assertEqual(r.status_code, 413)
        self.assertNotIn("content-length", {k.lower() for k in r.request.headers})
        with mock.patch.object(sincronia, "LIMITE_MANIFESTO", 4096):
            r = self.client.post("/api/sync/manifesto", content=pedacos(64 * 1024), headers=cab)
            self.assertEqual(r.status_code, 413)
        with mock.patch.object(sincronia, "MAXIMO_ITENS_MANIFESTO", 2):
            itens = [{"caminho": f"saidas/reserva/a/{i}.pdf", "tamanho": 1, "mtime_ns": 1} for i in range(3)]
            self.assertEqual(self.api("/api/sync/manifesto", {"arquivos": itens}).status_code, 413)
        with mock.patch.object(sincronia, "LIMITE_ARQUIVO", 4096):
            r = self.client.put("/api/sync/arquivo", content=pedacos(64 * 1024), headers=cab, params={
                "caminho": "saidas/reserva/a/b.pdf", "tamanho": 1000, "mtime_ns": 1, "sha256": "0" * 64})
            self.assertEqual(r.status_code, 413)
        self.assertFalse((self.servidor_saidas / "reserva" / "a" / "b.pdf").exists())
        self.assertEqual(list(self.tmp.rglob("*.parcial")), [])
        pagina = self.client.get("/login")  # também as rotas de sessão (login, tratativa)
        self.assertEqual(self.client.post("/login", content=pedacos(256 * 1024),
                                          headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code,
                         413)
        self.assertEqual(pagina.status_code, 200)


# ==========================================================================
# Envio do PC (sincronizar_servidor) e executor remoto
# ==========================================================================
class BasePC(BaseB):
    def setUp(self):
        super().setUp()
        self.pc = self.tmp / "pc"
        originais = {n: getattr(ss, n) for n in ("RAIZ", "PASTA_SAIDAS", "PASTA_DADOS")}
        self.addCleanup(lambda: [setattr(ss, n, v) for n, v in originais.items()])
        ss.RAIZ, ss.PASTA_SAIDAS, ss.PASTA_DADOS = self.pc, self.pc / "saidas", self.pc / "painel_dados"
        for caminho, conteudo in (("saidas/reserva/FULANA-reserva-1/FULANA-reserva-1.xlsx", b"planilha"),
                                  ("saidas/reserva/FULANA-reserva-1/rg.pdf", b"%PDF rg"),
                                  ("saidas/reserva/FULANA-reserva-1/~$FULANA-reserva-1.xlsx", b"trava do excel"),
                                  ("saidas/reserva/BELTRANO-reserva-2/c.pdf", b"%PDF c" * 1000),
                                  ("painel_dados/reservas_cv.json", b'{"reservas": {}}'),
                                  ("painel_dados/varreduras.jsonl", b"{}\n"),
                                  ("painel_dados/painel.db", b"banco do PC: nunca vai")):
            p = self.pc / caminho
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(conteudo)

    def no_servidor(self, caminho):
        return sincronia.destino(caminho, self.servidor_saidas, self.cfg.db.parent)

    def log(self):
        return "".join(p.read_text(encoding="utf-8") for p in (self.pc / "painel_dados" / "logs").glob("sync-*.txt"))


class EnvioDoPC(BasePC):
    def test_envia_so_o_que_falta_preserva_mtime_e_apaga_o_que_sumiu(self):
        self.assertTrue(ss.sincronizar("teste", cliente=self.cliente()))
        rg = self.no_servidor("saidas/reserva/FULANA-reserva-1/rg.pdf")
        self.assertEqual(rg.read_bytes(), b"%PDF rg")
        self.assertEqual(rg.stat().st_mtime_ns, (self.pc / "saidas/reserva/FULANA-reserva-1/rg.pdf").stat().st_mtime_ns)
        self.assertTrue(self.no_servidor("painel_dados/reservas_cv.json").exists())
        self.assertFalse((self.cfg.db.parent / "painel.db").read_bytes().startswith(b"banco do PC"))
        self.assertFalse(any(p.name.startswith("~$") for p in self.servidor_saidas.rglob("*")))
        sessao = SessaoDeTeste(self.client)
        self.assertTrue(ss.sincronizar("de novo", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao)))
        self.assertEqual([c for c in sessao.chamadas if c[0] == "PUT"], [], "nada mudou, nada enviado")
        (self.pc / "saidas/reserva/BELTRANO-reserva-2/c.pdf").unlink()
        (self.pc / "saidas/reserva/FULANA-reserva-1/rg.pdf").write_bytes(b"%PDF rg trocado")
        sessao = SessaoDeTeste(self.client)
        self.assertTrue(ss.sincronizar("mudou", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao)))
        self.assertEqual([c[1].split("?")[0] for c in sessao.chamadas if c[0] == "PUT"], ["/api/sync/arquivo"])
        self.assertEqual(rg.read_bytes(), b"%PDF rg trocado")
        self.assertFalse(self.no_servidor("saidas/reserva/BELTRANO-reserva-2/c.pdf").exists())
        self.assertIn("1 apagados no servidor", self.log())

    def test_carga_retomada_depois_da_queda(self):
        self.assertFalse(ss.sincronizar("primeira", cliente=self.cliente(cair_depois_de=3)), "manifesto + 2 envios")
        self.assertIn("falha", self.log())
        sessao = SessaoDeTeste(self.client)
        self.assertTrue(ss.sincronizar("retomada", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao)))
        self.assertEqual(len([c for c in sessao.chamadas if c[0] == "PUT"]), 3, "só os que faltavam (5 - 2)")

    def test_falha_nao_derruba_e_vai_para_o_log(self):
        self.assertFalse(ss.sincronizar("sem rede", cliente=self.cliente(cair_depois_de=0)))
        self.assertFalse(ss.sincronizar("chave errada", cliente=self.cliente(chave="errada")))
        quebrado = mock.Mock(post=mock.Mock(side_effect=RuntimeError("bug")))
        self.assertFalse(ss.sincronizar("bug", cliente=quebrado))
        log = self.log()
        self.assertIn("servidor indisponível", log)
        self.assertIn("HTTP 401", log)
        self.assertIn("erro inesperado", log)
        self.assertNotIn(CHAVE, log, "a chave nunca vai para o log")
        self.assertFalse((self.pc / "painel_dados" / "sync.trava").exists(), "a trava é solta")

    def test_so_https(self):
        # P1 da Turing: http mandaria a chave em claro. Sem exceção, nem por variável de ambiente.
        for url in ("http://10.0.0.48", "HTTP://servidor", "ftp://x", "10.0.0.48", "https://"):
            with self.subTest(url=url):
                with self.assertRaises(cs.ServidorIndisponivel):
                    cs.Cliente(url, CHAVE)
        self.assertEqual(cs.Cliente("HTTPS://10.0.0.48/", CHAVE).url, "HTTPS://10.0.0.48")
        env = {"SECRETARIA_MODO_ENVIO": "remoto", "SECRETARIA_MODO_EXECUTOR": "remoto",
               "SECRETARIA_SERVIDOR_URL": "http://10.0.0.48",
               "SECRETARIA_EXECUTOR_CHAVE": CHAVE}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(ss.sincronizar("http"))
            with mock.patch.object(ex, "Banco", side_effect=AssertionError("abriu o painel.db")), \
                    mock.patch.object(ex, "instancia_unica", return_value=_Unico()):
                self.assertEqual(ex.main(["--uma-vez"], eco=False), 2)
        self.assertIn("precisa ser https", self.log())
        self.assertNotIn(CHAVE, self.log())

    def test_manifesto_incompleto_nao_apaga_nada(self):
        self.assertTrue(ss.sincronizar("primeira", cliente=self.cliente()))
        c_pdf = self.no_servidor("saidas/reserva/BELTRANO-reserva-2/c.pdf")
        estat, listar = ss._estat, ss._listar

        def stat_que_falha(entrada):
            if entrada.name == "c.pdf":
                raise PermissionError("em uso")
            return estat(entrada)
        with mock.patch.object(ss, "_estat", stat_que_falha):
            self.assertFalse(ss.sincronizar("leitura falhou", cliente=self.cliente()))
        self.assertTrue(c_pdf.exists(), "o arquivo que o PC não conseguiu ler continua no servidor")

        def pasta_sem_acesso(pasta):  # P1c: is_dir() engoliria o erro e a pasta sumiria do manifesto
            if Path(pasta).name == "BELTRANO-reserva-2":
                raise PermissionError("acesso negado")
            return listar(pasta)
        with mock.patch.object(ss, "_listar", pasta_sem_acesso):
            r = ss.sincronizar_detalhado("pasta sem acesso", cliente=self.cliente())
        self.assertFalse(r.ok)
        self.assertIn("saidas/reserva/BELTRANO-reserva-2/", r.nao_entregues)
        self.assertFalse(r.reserva_entregue(2))
        self.assertTrue(r.reserva_entregue(1))
        self.assertTrue(c_pdf.exists(), "pasta que o PC não conseguiu listar não é apagada no servidor")
        with mock.patch.object(ss, "_listar", side_effect=OSError("disco")):
            r = ss.sincronizar_detalhado("enumeração falhou", cliente=self.cliente())
        self.assertFalse(r.reserva_entregue(1), "sem conseguir listar saidas/reserva, nenhuma reserva está garantida")
        self.assertTrue(c_pdf.exists())
        shutil.rmtree(self.pc / "saidas")
        self.assertFalse(ss.sincronizar("sem saidas/reserva", cliente=self.cliente()))
        self.assertTrue(c_pdf.exists(), "pasta ausente no PC não vira 'apague tudo'")
        self.assertEqual(sum(1 for _ in self.servidor_saidas.rglob("*.pdf")), 2)
        self.assertIn("manifesto incompleto", self.log())
        self.assertIn("nada será apagado", self.log())

    def test_conferencia_completa_por_sha(self):
        # P2 da Turing: arquivo corrigido com o mesmo tamanho e o mesmo mtime não sobe na rodada rápida.
        self.assertTrue(ss.sincronizar("primeira", cliente=self.cliente(), completo=False))
        rg = self.no_servidor("saidas/reserva/FULANA-reserva-1/rg.pdf")
        st = rg.stat()
        rg.write_bytes(b"%PDF XX")  # mesmo tamanho de "%PDF rg"
        os.utime(rg, ns=(st.st_mtime_ns, st.st_mtime_ns))
        self.assertTrue(ss.sincronizar("rápida", cliente=self.cliente(), completo=False))
        self.assertEqual(rg.read_bytes(), b"%PDF XX", "a rápida só olha tamanho e mtime")
        self.assertTrue(ss.sincronizar("completa", cliente=self.cliente(), completo=True))
        self.assertEqual(rg.read_bytes(), b"%PDF rg", "a completa compara o sha256 e corrige")
        self.assertIn("conferência completa (sha256)", self.log())

    def test_conferencia_do_dia(self):
        marcador = self.pc / "painel_dados" / "sync_conferencia.txt"
        self.assertFalse(ss.conferencia_do_dia_pendente(datetime(2026, 10, 21, 2, 59)))
        self.assertTrue(ss.conferencia_do_dia_pendente(datetime(2026, 10, 21, 3, 0)))
        with mock.patch.object(ss, "conferencia_do_dia_pendente", return_value=True):
            self.assertTrue(ss.sincronizar("5 min", cliente=self.cliente()))
        self.assertIn("conferência completa (sha256)", self.log())
        self.assertEqual(marcador.read_text(encoding="utf-8"), datetime.now().strftime("%Y-%m-%d"))
        agora = datetime.now().replace(hour=4)
        self.assertFalse(ss.conferencia_do_dia_pendente(agora), "uma vez por dia")
        self.assertTrue(ss.conferencia_do_dia_pendente(agora + timedelta(days=1)))

    def test_modo_explicito_e_falha_fechada(self):
        # P1b + ensaio: modo POR COMPONENTE; configuração parcial nunca cai no painel.db local.
        url = {"SECRETARIA_SERVIDOR_URL": "https://10.0.0.48", "SECRETARIA_EXECUTOR_CHAVE": CHAVE}
        validos = (({}, ("local", "local")),
                   (dict(url, SECRETARIA_MODO_ENVIO="remoto", SECRETARIA_MODO_EXECUTOR="local"), ("remoto", "local")),
                   (dict(url, SECRETARIA_MODO_ENVIO="remoto", SECRETARIA_MODO_EXECUTOR="remoto",
                         SECRETARIA_CA_BUNDLE="ca.crt"), ("remoto", "remoto")),
                   (dict(url, SECRETARIA_MODO_ENVIO="local", SECRETARIA_MODO_EXECUTOR="local"), ("local", "local")))
        for env, esperado in validos:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual((cs.modo("envio"), cs.modo("executor")), esperado)
        parciais = ({"SECRETARIA_SERVIDOR_URL": "https://10.0.0.48"},
                    {"SECRETARIA_EXECUTOR_CHAVE": CHAVE},
                    {"SECRETARIA_CA_BUNDLE": "ca.crt"},
                    url,
                    {"SECRETARIA_MODO_ENVIO": "remoto", "SECRETARIA_MODO_EXECUTOR": "remoto",
                     "SECRETARIA_SERVIDOR_URL": "https://10.0.0.48"},
                    {"SECRETARIA_MODO_ENVIO": "remoto", "SECRETARIA_MODO_EXECUTOR": "remoto",
                     "SECRETARIA_EXECUTOR_CHAVE": CHAVE},
                    dict(url, SECRETARIA_MODO_ENVIO="nuvem", SECRETARIA_MODO_EXECUTOR="nuvem"))
        for env in parciais:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                for componente in ("envio", "executor"):
                    with self.assertRaises(cs.ConfiguracaoInvalida) as erro:
                        cs.modo(componente)
                    self.assertNotIn(CHAVE, str(erro.exception))
                self.assertFalse(ss.sincronizar("parcial"))
                with mock.patch.object(ex, "Banco", side_effect=AssertionError("abriu o painel.db local")), \
                        mock.patch.object(ex, "instancia_unica", return_value=_Unico()):
                    self.assertEqual(ex.main(["--uma-vez"], eco=False), 2)
                with mock.patch.object(tr.vd, "rodar", side_effect=AssertionError("a varredura rodou")), \
                        mock.patch.object(tr.vd, "atualizar_funil", side_effect=AssertionError("o funil rodou")):
                    for argv in (["varredura_diaria.py"], ["varredura_diaria.py", "--atualizar-funil"]):
                        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr"):
                            self.assertEqual(tr.vd.main(), 2)
        # executor remoto com o envio local: ele concluiria sem enviar a planilha.
        with mock.patch.dict(os.environ, dict(url, SECRETARIA_MODO_ENVIO="local", SECRETARIA_MODO_EXECUTOR="remoto"),
                             clear=True):
            self.assertEqual(cs.modo("envio"), "local")
            with self.assertRaises(cs.ConfiguracaoInvalida):
                cs.modo("executor")
            with mock.patch.object(ex, "Banco", side_effect=AssertionError("abriu o painel.db local")), \
                    mock.patch.object(ex, "instancia_unica", return_value=_Unico()):
                self.assertEqual(ex.main(["--uma-vez"], eco=False), 2)
        self.assertIn("configuração do servidor inválida - nada enviado", self.log())
        self.assertNotIn(CHAVE, self.log())
        self.assertFalse(tr.vd.ARQ_TRAVA.exists())

    def test_ensaio_envio_remoto_executor_local(self):
        # No ensaio: o executor residente segue no painel.db do PC; UM executor remoto (--uma-vez),
        # com a própria instância, atende o pedido de teste do servidor.
        env = {"SECRETARIA_SERVIDOR_URL": "https://10.0.0.48", "SECRETARIA_EXECUTOR_CHAVE": CHAVE,
               "SECRETARIA_MODO_ENVIO": "remoto", "SECRETARIA_MODO_EXECUTOR": "local"}
        nomes, bancos = [], []
        instancia = lambda nome=ex.NOME_DA_INSTANCIA: nomes.append(nome) or _Unico()  # noqa: E731
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ex, "instancia_unica", instancia), \
                mock.patch.object(ex, "Banco", lambda caminho: bancos.append(caminho) or self.banco), \
                mock.patch.object(ex.Executor, "atender_um", return_value=None), mock.patch("sys.stdout"):
            self.assertEqual(ex.main(["--uma-vez"], eco=False), 0)
            self.assertEqual(len(bancos), 1, "o executor local usa o painel.db do PC")
            with mock.patch.dict(os.environ, {"SECRETARIA_MODO_EXECUTOR": "remoto"}):
                self.assertEqual(ex.main(["--uma-vez"], eco=False), 0)
            self.assertEqual(len(bancos), 1, "o remoto não abre o painel.db")
        self.assertEqual(nomes, [ex.NOME_DA_INSTANCIA, ex.NOME_DA_INSTANCIA + "-Remoto"],
                         "instâncias separadas: o remoto do ensaio roda com o local no ar")

    def test_comparar_pc_e_servidor(self):
        self.assertTrue(ss.sincronizar("carga", cliente=self.cliente()))
        texto = io.StringIO()
        self.assertEqual(ss.comparar_com_o_servidor(cliente=self.cliente(), saida=texto), 0)
        self.assertIn("PC: 5 arquivos", texto.getvalue())
        self.assertIn("servidor: 5 arquivos", texto.getvalue())
        self.assertIn("BATEM", texto.getvalue())
        rg = self.no_servidor("saidas/reserva/FULANA-reserva-1/rg.pdf")
        st = rg.stat()
        rg.write_bytes(b"%PDF XX")  # mesmo tamanho e mesmo mtime: só o sha256 pega
        os.utime(rg, ns=(st.st_mtime_ns, st.st_mtime_ns))
        self.no_servidor("saidas/reserva/BELTRANO-reserva-2/c.pdf").unlink()
        extra = self.servidor_saidas / "reserva" / "VELHA-reserva-9" / "x.pdf"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"so no servidor")
        texto = io.StringIO()
        self.assertEqual(ss.comparar_com_o_servidor(cliente=self.cliente(), saida=texto), 3)
        relatorio = texto.getvalue()
        self.assertRegex(relatorio, r"faltam no servidor: 1\n  saidas/reserva/BELTRANO-reserva-2/c.pdf")
        self.assertRegex(relatorio, r"diferentes \(tamanho, data ou sha256\): 1\n  saidas/reserva/FULANA-reserva-1/rg.pdf")
        self.assertRegex(relatorio, r"só no servidor \(sumiram do PC\): 1\n  saidas/reserva/VELHA-reserva-9/x.pdf")
        self.assertTrue(extra.exists(), "comparar não apaga nada")
        self.assertEqual(rg.read_bytes(), b"%PDF XX", "comparar não envia nada")

    def test_publicacao_por_ultimo(self):
        # P1d: se uma planilha falha, a listagem e o varreduras.jsonl não sobem nesta rodada.
        class FalhaNumArquivo(SessaoDeTeste):
            def request(self, metodo, url, **k):
                if metodo == "PUT" and (k.get("params") or {}).get("caminho", "").endswith("rg.pdf"):
                    self.chamadas.append((metodo, "/api/sync/arquivo"))
                    return type("R", (), {"status_code": 500, "json": lambda self: {"detail": "erro"}})()
                return super().request(metodo, url, **k)
        r = ss.sincronizar_detalhado("rg falha", cliente=cs.Cliente("https://testserver", CHAVE,
                                                                    sessao=FalhaNumArquivo(self.client)))
        self.assertFalse(r.ok)
        self.assertTrue(self.no_servidor("saidas/reserva/BELTRANO-reserva-2/c.pdf").exists(), "as outras sobem")
        for nome in ("reservas_cv.json", "varreduras.jsonl"):
            no_servidor, no_pc = self.no_servidor(f"painel_dados/{nome}"), self.pc / "painel_dados" / nome
            self.assertFalse(no_servidor.exists() and no_servidor.read_bytes() == no_pc.read_bytes(),
                             f"{nome} só depois das planilhas")
            self.assertIn(f"painel_dados/{nome}", r.nao_entregues)
        self.assertFalse(r.reserva_entregue(1))
        self.assertTrue(r.reserva_entregue(2))
        self.assertIn("publicação adiada", self.log())
        self.assertTrue(ss.sincronizar("de novo", cliente=self.cliente()))
        self.assertEqual(self.no_servidor("painel_dados/reservas_cv.json").read_bytes(), b'{"reservas": {}}')

    def test_link_ou_reparse_point_marca_incompleto(self):
        # P2 da Turing: link/reparse não pode sumir do manifesto em silêncio (e virar exclusão no servidor).
        self.assertTrue(ss.sincronizar("primeira", cliente=self.cliente()))
        rg = self.no_servidor("saidas/reserva/FULANA-reserva-1/rg.pdf")
        c_pdf = self.no_servidor("saidas/reserva/BELTRANO-reserva-2/c.pdf")
        estat = ss._estat

        def com_links(entrada):
            st = estat(entrada)
            if entrada.name == "rg.pdf":  # reparse point (ex.: OneDrive "só online")
                return types.SimpleNamespace(st_mode=st.st_mode, st_size=st.st_size, st_mtime_ns=st.st_mtime_ns,
                                             st_file_attributes=ss.FILE_ATTRIBUTE_REPARSE_POINT)
            if entrada.name == "BELTRANO-reserva-2":  # link simbólico para outra pasta
                return types.SimpleNamespace(st_mode=stat_mod.S_IFLNK | 0o777, st_size=0, st_mtime_ns=0)
            return st
        with mock.patch.object(ss, "_estat", com_links):
            r = ss.sincronizar_detalhado("com links", cliente=self.cliente())
        self.assertFalse(r.ok)
        self.assertIn("saidas/reserva/FULANA-reserva-1/rg.pdf", r.nao_entregues)
        self.assertIn("saidas/reserva/BELTRANO-reserva-2/", r.nao_entregues)
        self.assertTrue(rg.exists() and c_pdf.exists(), "nada sumiu do servidor")
        self.assertIn("link ou reparse point", self.log())
        with mock.patch.object(ss.os, "lstat", side_effect=lambda p, *a, **k: types.SimpleNamespace(
                st_mode=stat_mod.S_IFDIR, st_file_attributes=ss.FILE_ATTRIBUTE_REPARSE_POINT)
                if Path(p).name == "reserva" else os.stat(p, follow_symlinks=False)):
            r = ss.sincronizar_detalhado("raiz é junção", cliente=self.cliente())
        self.assertIn("saidas/reserva/", r.nao_entregues)
        self.assertTrue(rg.exists() and c_pdf.exists())

    def test_falha_de_envio_depois_do_manifesto_nada_some_nem_publica(self):
        # P2 da Turing / ensaio E7: as exclusões só acontecem no fim; a listagem só depois das planilhas.
        self.assertTrue(ss.sincronizar("primeira", cliente=self.cliente()))
        c_pdf = self.no_servidor("saidas/reserva/BELTRANO-reserva-2/c.pdf")
        listagem_antes = self.no_servidor("painel_dados/reservas_cv.json").read_bytes()
        (self.pc / "saidas/reserva/BELTRANO-reserva-2/c.pdf").unlink()  # sumiu do PC: o servidor apagaria
        (self.pc / "saidas/reserva/FULANA-reserva-1/rg.pdf").write_bytes(b"%PDF rg novo")
        (self.pc / "painel_dados/reservas_cv.json").write_bytes(b'{"reservas": {"1": {}}}')
        sessao = SessaoDeTeste(self.client)
        r = ss.sincronizar_detalhado("E7", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao),
                                     falhar_em="rg.pdf")
        self.assertFalse(r.ok)
        self.assertTrue(c_pdf.exists(), "nada some quando a rodada não termina inteira")
        self.assertNotIn(("POST", "/api/sync/concluir"), sessao.chamadas)
        self.assertEqual(self.no_servidor("painel_dados/reservas_cv.json").read_bytes(), listagem_antes,
                         "a listagem nova não foi publicada")
        self.assertIn("falha forçada (ensaio", self.log())
        self.assertIn("exclusões adiadas", self.log())
        self.assertIn("publicação adiada", self.log())
        sessao = SessaoDeTeste(self.client)
        self.assertTrue(ss.sincronizar("de novo", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao)))
        ordem = [c[1].split("?")[0] for c in sessao.chamadas if c[1].startswith("/api/sync/")]
        self.assertEqual(ordem[-1], "/api/sync/concluir", "a exclusão é a última chamada do envio")
        self.assertFalse(c_pdf.exists())
        self.assertEqual(self.no_servidor("painel_dados/reservas_cv.json").read_bytes(), b'{"reservas": {"1": {}}}')

    def test_artefato_obrigatorio_ausente_bloqueia_concluir_e_exclusoes(self):
        # 2ª confirmação da Turing: sem reservas_cv.json (ou varreduras.jsonl) o manifesto não é completo.
        self.assertTrue(ss.sincronizar("primeira", cliente=self.cliente()))
        c_pdf = self.no_servidor("saidas/reserva/BELTRANO-reserva-2/c.pdf")
        (self.pc / "saidas/reserva/BELTRANO-reserva-2/c.pdf").unlink()
        for nome in ss.ARQUIVOS_OBRIGATORIOS:
            with self.subTest(nome=nome):
                local = self.pc / "painel_dados" / nome
                guardado = local.read_bytes()
                local.unlink()
                sessao = SessaoDeTeste(self.client)
                r = ss.sincronizar_detalhado("sem " + nome, cliente=cs.Cliente("https://testserver", CHAVE,
                                                                              sessao=sessao))
                self.assertFalse(r.ok)
                self.assertIn(f"painel_dados/{nome}", r.nao_entregues)
                self.assertNotIn(("POST", "/api/sync/concluir"), sessao.chamadas)
                self.assertTrue(c_pdf.exists(), "sem concluir, nada é apagado")
                local.write_bytes(guardado)
        self.assertIn("obrigatório ausente", self.log())
        (self.pc / "painel_dados" / "ultima_varredura_ok.txt").unlink(missing_ok=True)  # opcional
        self.assertTrue(ss.sincronizar("completo de novo", cliente=self.cliente()))
        self.assertFalse(c_pdf.exists(), "com tudo no lugar, a exclusão acontece no fim")

    def test_linha_de_comando_do_ensaio(self):
        chamado = {}

        def falso(motivo, esperar_s=0, cliente=None, completo=None, falhar_em=None):
            chamado.update(motivo=motivo, falhar_em=falhar_em)
            return ss.Resultado(ok=False, nao_entregues=["saidas/reserva/X-reserva-1/a.xlsx",
                                                         "painel_dados/reservas_cv.json"])
        with mock.patch.object(ss, "sincronizar_detalhado", falso), \
                mock.patch.object(sys, "argv", ["sincronizar_servidor.py", "--ensaio-falhar-em", "reserva-1"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as saida:
            self.assertEqual(ss.main(), 1)
        self.assertEqual(chamado["falhar_em"], "reserva-1")
        self.assertIn("NÃO foi publicada", saida.getvalue())

    def test_modo_local_nao_faz_nada(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ss.sincronizar("local"))
        self.assertFalse((self.pc / "painel_dados" / "logs").exists())

    def test_uma_sincronizacao_por_vez(self):
        trava = self.pc / "painel_dados" / "sync.trava"
        trava.write_text("123")
        self.assertFalse(ss.sincronizar("concorrente", cliente=self.cliente()))
        self.assertIn("outra sincronização em andamento", self.log())
        velha = time.time() - ss.TRAVA_VELHA_APOS_S - 60
        os.utime(trava, (velha, velha))
        self.assertTrue(ss.sincronizar("trava abandonada", cliente=self.cliente()))

    def test_executor_remoto_envia_antes_de_concluir(self):
        self.banco.pedir_reanalise(34510, "painel", "ana", tr.RESERVAS)
        sessao = SessaoDeTeste(self.client)
        fila = ex.FilaRemota(cs.Cliente("https://testserver", CHAVE, sessao=sessao), eco=False)
        ordem = []
        with mock.patch.object(ex, "_analisar", lambda idr: ("concluido", "2 pasta(s) analisada(s)", None)), \
                mock.patch.object(ex.sincronizar_servidor, "sincronizar_detalhado",
                                  lambda motivo, esperar_s=0: ordem.append(("sync", len(sessao.chamadas)))
                                  or ss.Resultado(ok=True)):
            status = ex.Executor(fila, eco=False).atender_um()
        self.assertEqual(status, "concluido")
        rotas = [c[1] for c in sessao.chamadas]
        self.assertEqual(rotas, ["/api/executor/estado", "/api/executor/proximo", "/api/executor/concluir"])
        self.assertEqual(ordem, [("sync", 2)], "o envio vem antes do concluir")
        self.assertEqual(self.pedidos()[0]["status"], "concluido")
        self.assertFalse(tr.vd.ARQ_TRAVA.exists(), "a trava do PC foi solta")

    def test_executor_so_conclui_se_a_reserva_inteira_subiu(self):
        # P1d: envio que falhou para ESTA reserva = pedido falhou com o motivo (não conclui, não volta à fila).
        casos = (
            (ss.Resultado(ok=False, nao_entregues=["saidas/reserva/FULANA-reserva-34510/rg.pdf"]), "falhou"),
            (ss.Resultado(ok=False, nao_entregues=["saidas/reserva/FULANA-2o Promissario-reserva-34510/"]),
             "falhou"),
            (ss.Resultado(ok=False, erro="servidor indisponível"), "falhou"),
            (ss.Resultado(ok=False, nao_entregues=["saidas/reserva/OUTRA-reserva-34520/x.pdf"]), "concluido"),
            (ss.Resultado(ok=True), "concluido"),
        )
        for envio, esperado in casos:
            with self.subTest(envio=envio):
                con = sqlite3.connect(self.cfg.db)
                con.execute("DELETE FROM pedidos_reanalise")
                con.commit()
                con.close()
                self.banco.pedir_reanalise(34510, "painel", "ana", tr.RESERVAS)
                fila = ex.FilaRemota(self.cliente(), eco=False)
                with mock.patch.object(ex, "_analisar", lambda idr: ("concluido", "2 pasta(s) analisada(s)", None)), \
                        mock.patch.object(ex.sincronizar_servidor, "sincronizar_detalhado",
                                          lambda motivo, esperar_s=0, e=envio: e):
                    self.assertEqual(ex.Executor(fila, eco=False).atender_um(), esperado)
                pedido = self.pedidos()[0]
                self.assertEqual(pedido["status"], esperado)
                if esperado == "falhou":
                    self.assertEqual(pedido["erro"], ex.MOTIVO_ENVIO_FALHOU)
                    self.assertIsNone(pedido["resultado"])

    def test_busca_falha_se_a_listagem_nao_foi_publicada(self):
        busca, _ = self.banco.pedir_busca_do_funil("painel", "ana")
        fila = ex.FilaRemota(self.cliente(), eco=False)
        envio = ss.Resultado(ok=False, nao_entregues=["saidas/reserva/X-reserva-1/a.pdf",
                                                      "painel_dados/reservas_cv.json"])
        with mock.patch.object(tr.vd, "rodar_funil_com_a_trava", lambda: ("funil ok", 0)), \
                mock.patch.object(ex.sincronizar_servidor, "sincronizar_detalhado", lambda motivo, esperar_s=0: envio):
            self.assertEqual(ex.Executor(fila, eco=False, saidas=self.pc / "saidas").atender_um(), "falhou")
        self.assertEqual(self.pedidos()[0]["erro"], ex.MOTIVO_LISTAGEM_NAO_CHEGOU)

    def test_executor_remoto_sem_rede_nao_pega_a_trava_nem_quebra(self):
        self.banco.pedir_reanalise(34510, "painel", "ana", tr.RESERVAS)
        fila = ex.FilaRemota(self.cliente(cair_depois_de=0), eco=False)
        self.assertIsNone(ex.Executor(fila, eco=False).atender_um())
        self.assertFalse(tr.vd.ARQ_TRAVA.exists())
        self.assertEqual(self.pedidos()[0]["status"], "na_fila")

    def test_executor_manda_o_modo_da_trava_do_pc(self):
        self.banco.pedir_reanalise(34510, "painel", "ana", tr.RESERVAS)
        self.trava_viva("varredura")
        fila = ex.FilaRemota(self.cliente(), eco=False)
        self.assertEqual(ex.Executor(fila, eco=False).atender_um(), "trava_ocupada")
        self.assertEqual(self.banco.estado_do_executor()["modo"], "varredura")
        self.assertEqual(self.pedidos()[0]["status"], "na_fila")

    def test_main_remoto_nao_abre_o_painel_db(self):
        env = {"SECRETARIA_MODO_ENVIO": "remoto", "SECRETARIA_MODO_EXECUTOR": "remoto",
               "SECRETARIA_SERVIDOR_URL": "https://10.0.0.48",
               "SECRETARIA_EXECUTOR_CHAVE": CHAVE}
        with mock.patch.dict(os.environ, env), mock.patch.object(ex, "Banco", side_effect=AssertionError("abriu")), \
                mock.patch.object(ex.Executor, "atender_um", return_value=None), \
                mock.patch.object(ex, "instancia_unica", return_value=_Unico()), \
                mock.patch("sys.stdout"):
            self.assertEqual(ex.main(["--uma-vez"], eco=False), 0)


# ==========================================================================
# Backup do painel.db (só no servidor) e a cópia no PC
# ==========================================================================
class BackupDoBanco(BasePC):
    def setUp(self):
        super().setUp()
        self.pasta_backups = self.cfg.db.parent / "backups"

    def usuarios(self, caminho):
        con = sqlite3.connect(caminho)
        try:
            return sorted(r[0] for r in con.execute("SELECT login FROM usuarios"))
        finally:
            con.close()

    def test_backup_integro_e_um_por_dia(self):
        hoje = datetime.now().replace(hour=2, minute=5)
        self.assertIsNone(backup_mod.rodar_se_pendente(self.cfg.db, hoje.replace(hour=1)), "antes das 2h, não")
        feito = backup_mod.rodar_se_pendente(self.cfg.db, hoje)
        self.assertEqual(feito.name, f"painel-{hoje:%Y-%m-%d}.db")
        self.assertEqual(backup_mod.integridade(feito), "ok")
        self.assertEqual(self.usuarios(feito), self.usuarios(self.cfg.db))
        self.assertIsNone(backup_mod.rodar_se_pendente(self.cfg.db, hoje.replace(hour=23)), "um por dia")
        log = (self.cfg.db.parent / "logs" / "backup.txt").read_text(encoding="utf-8")
        self.assertIn("backup ok", log)
        self.assertIn("integrity_check ok", log)
        self.assertEqual(list(self.pasta_backups.glob("*.parcial")), [])

    def test_backup_que_nao_passa_na_integridade_nao_fica(self):
        with mock.patch.object(backup_mod, "integridade", return_value="*** in database main ***"):
            self.assertIsNone(backup_mod.rodar_se_pendente(self.cfg.db, datetime.now().replace(hour=3)))
        self.assertEqual(backup_mod.backups(self.pasta_backups), [])
        self.assertEqual(list(self.pasta_backups.glob("*.parcial")), [])
        self.assertFalse(backup_mod.estado(self.cfg.db.parent)["ultimo_resultado"]["ok"])
        self.assertIn("backup FALHOU", (self.cfg.db.parent / "logs" / "backup.txt").read_text(encoding="utf-8"))

    def test_guarda_14(self):
        modelo = backup_mod.rodar_se_pendente(self.cfg.db, datetime(2026, 10, 1, 2))
        for dia in range(2, 21):
            shutil.copy(modelo, self.pasta_backups / f"painel-2026-10-{dia:02d}.db")
        (self.pasta_backups / "outro-arquivo.db").write_bytes(b"x")
        apagados = backup_mod.rotacionar(self.pasta_backups)
        self.assertEqual(len(apagados), 6)
        self.assertEqual([p.name for p in backup_mod.backups(self.pasta_backups)],
                         [f"painel-2026-10-{d:02d}.db" for d in range(20, 6, -1)])
        self.assertTrue((self.pasta_backups / "outro-arquivo.db").exists(), "só mexe nos backups")

    def test_o_servico_faz_o_backup_sozinho(self):
        self.client.close()
        app = criar_app(dataclasses.replace(self.cfg, backup_diario=True), templates_dir=tp.TEMPLATES_FAKE)
        with mock.patch.object(backup_mod, "HORA_DO_BACKUP", 0), TestClient(app):  # sobe: confere na hora
            limite = time.monotonic() + 10
            while not backup_mod.backups(self.pasta_backups) and time.monotonic() < limite:
                time.sleep(0.05)
        self.assertEqual(len(backup_mod.backups(self.pasta_backups)), 1)
        self.client = TestClient(self.app, follow_redirects=False)

    def test_estado_do_backup_para_o_admin(self):
        e = backup_mod.estado(self.cfg.db.parent)
        self.assertIn("nenhum backup ainda", e["alertas"])
        backup_mod.rodar_se_pendente(self.cfg.db, datetime.now().replace(hour=2))
        e = backup_mod.estado(self.cfg.db.parent)
        self.assertEqual((e["alertas"], e["ultimo_resultado"]["ok"]), ([], True))
        self.assertLess(e["idade_horas"], 1)
        uso = type("U", (), {"free": int(1.5 * 2**30)})()
        with mock.patch.object(backup_mod.shutil, "disk_usage", return_value=uso):
            self.assertEqual(backup_mod.estado(self.cfg.db.parent)["alertas"], ["pouco espaço: 1.5 GB livres"])
        velho = time.time() - 40 * 3600
        os.utime(backup_mod.backups(self.pasta_backups)[0], (velho, velho))
        self.assertTrue(backup_mod.estado(self.cfg.db.parent)["alertas"][0].startswith("último backup bom há 40 h"))
        # No site, com os templates de verdade: só o admin vê.
        self.client.close()
        app = criar_app(dataclasses.replace(self.cfg, backup_diario=True))
        tp.auth.criar_usuario(app.state.banco, "chefe", "Chefe", "admin", tp.SENHA)
        for login, ve in (("ana", False), ("chefe", True)):
            cliente = TestClient(app, follow_redirects=False)
            pagina = cliente.get("/login")
            csrf = re.search(r'name="csrf" value="([^"]+)"', pagina.text).group(1)
            cliente.post("/login", data={"login": login, "senha": tp.SENHA, "next": "/", "csrf": csrf})
            html = cliente.get("/").text
            self.assertEqual("data-backup" in html, ve, login)
            if ve:
                self.assertIn("último backup bom há 40 h", html)
            cliente.close()
        self.client = TestClient(self.app, follow_redirects=False)

    def test_copiar_e_conferir_banco_pelo_terminal(self):
        destino = self.tmp / "copia.db"
        with mock.patch.dict(os.environ, {"SECRETARIA_PAINEL_DB": str(self.cfg.db),
                                          "SECRETARIA_SAIDAS": str(self.servidor_saidas)}, clear=True), \
                mock.patch("sys.stdout"):
            tp.cli.main(["copiar-banco", "--para", str(destino)])
            self.assertEqual(self.usuarios(destino), self.usuarios(self.cfg.db))
            with self.assertRaises(SystemExit):
                tp.cli.main(["copiar-banco", "--para", str(destino)])  # não sobrescreve
            tp.cli.main(["conferir-banco", "--arquivo", str(destino)])
            destino.write_bytes(b"isto nao e um banco" * 100)
            with self.assertRaises(SystemExit):
                tp.cli.main(["conferir-banco", "--arquivo", str(destino)])

    def test_configuracao(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(tp.config_mod.carregar().backup_diario, "no PC, nada muda")
        with mock.patch.dict(os.environ, {"SECRETARIA_EXECUTOR_CHAVE_HASH": HASH}, clear=True):
            self.assertTrue(tp.config_mod.carregar().backup_diario)
        with mock.patch.dict(os.environ, {"SECRETARIA_BACKUP_DIARIO": "1"}, clear=True):
            self.assertTrue(tp.config_mod.carregar().backup_diario)

    def test_rota_do_backup(self):
        self.assertEqual(self.client.get("/api/executor/backup").status_code, 401)
        self.assertEqual(self.client.get("/api/executor/backup", headers={"Authorization": "Bearer x"}).status_code,
                         401)
        cab = {"Authorization": f"Bearer {CHAVE}"}
        self.assertEqual(self.client.get("/api/executor/backup", headers=cab).status_code, 404, "ainda não há")
        backup_mod.rodar_se_pendente(self.cfg.db, datetime(2026, 10, 19, 2))
        novo = backup_mod.rodar_se_pendente(self.cfg.db, datetime(2026, 10, 20, 2))
        r = self.client.get("/api/executor/backup", headers=cab)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-backup-sha256"], hashlib.sha256(r.content).hexdigest())
        self.assertEqual(r.content, novo.read_bytes(), "o mais recente")
        self.assertIn('filename="painel-2026-10-20.db"', r.headers["content-disposition"])

    def test_pc_baixa_confere_e_guarda_uma_vez_por_dia(self):
        backup_mod.rodar_se_pendente(self.cfg.db, datetime.now().replace(hour=2))
        pasta_pc = self.pc / "painel_dados" / "backups_servidor"
        pasta_pc.mkdir(parents=True)
        for dia in range(1, 16):  # 15 antigos: depois do novo, ficam 14
            (pasta_pc / f"painel-2026-09-{dia:02d}.db").write_bytes(b"antigo")
        with mock.patch.object(ss, "HORA_DO_BACKUP_DO_SERVIDOR", 0):
            sessao = SessaoDeTeste(self.client)
            self.assertTrue(ss.sincronizar("1ª do dia", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao)))
            self.assertIn(("GET", "/api/executor/backup"), sessao.chamadas)
            baixado = pasta_pc / f"painel-{datetime.now():%Y-%m-%d}.db"
            self.assertEqual(backup_mod.sha256(baixado), backup_mod.sha256(backup_mod.backups(self.pasta_backups)[0]))
            self.assertEqual(self.usuarios(baixado), self.usuarios(self.cfg.db))
            self.assertEqual(len(backup_mod.backups(pasta_pc)), 14)
            self.assertFalse((pasta_pc / "painel-2026-09-01.db").exists())
            sessao = SessaoDeTeste(self.client)
            self.assertTrue(ss.sincronizar("2ª do dia", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao)))
            self.assertNotIn(("GET", "/api/executor/backup"), sessao.chamadas, "uma vez por dia")
        self.assertIn("sha256 e integrity_check ok", self.log())

    def test_pc_sha_divergente_ou_sem_backup_so_loga(self):
        pasta_pc = self.pc / "painel_dados" / "backups_servidor"
        with mock.patch.object(ss, "HORA_DO_BACKUP_DO_SERVIDOR", 0):
            self.assertTrue(ss.sincronizar("sem backup no servidor", cliente=self.cliente()),
                            "o envio dos arquivos não depende do backup")
            backup_mod.rodar_se_pendente(self.cfg.db, datetime.now().replace(hour=2))
            sessao = SessaoDeTeste(self.client)
            sessao.estragar = True
            self.assertTrue(ss.sincronizar("corrompido", cliente=cs.Cliente("https://testserver", CHAVE, sessao=sessao)))
        self.assertEqual(backup_mod.backups(pasta_pc), [], "o que não confere não fica")
        self.assertEqual(list(pasta_pc.glob("*.parcial")), [])
        self.assertIn("não baixado - /api/executor/backup: HTTP 404", self.log())
        self.assertIn("sha256 não confere", self.log())
        self.assertTrue(ss.backup_do_dia_pendente(datetime.now().replace(hour=4)), "tenta de novo na próxima")


# ==========================================================================
# Virada: congelar / descongelar os escritores do PC
# ==========================================================================
class CongelarPC(BasePC):
    def setUp(self):
        super().setUp()
        self.comandos, self.encerrados = [], []
        self.processos = [[{"pid": 10, "linha": "pythonw painel_servidor.pyw"},
                           {"pid": 11, "linha": "pythonw executor_reanalise.pyw"}], []]
        self.falhar_tarefa = None
        self.respostas_powershell = []

        def rodar(comando):
            self.comandos.append(comando)
            if comando[0] == "powershell":
                return self.respostas_powershell.pop(0)
            return (1, "Acesso negado") if self.falhar_tarefa and self.falhar_tarefa in comando[3] else (0, "OK")
        for nome, valor in (("_rodar", rodar), ("_encerrar", self.encerrados.append),
                            ("_processos_do_projeto", lambda: self.processos.pop(0) if self.processos else [])):
            p = mock.patch.object(congelar_mod, nome, valor)
            p.start()
            self.addCleanup(p.stop)
        self.saida = []

    def tarefas(self, acao):
        return [c[3].rsplit("\\", 1)[-1] for c in self.comandos if c[0] == "schtasks" and c[-1] == acao]

    def test_congela_e_confere(self):
        self.assertTrue(congelar_mod.congelar(self.cfg.db, self.saida.append))
        self.assertEqual(self.tarefas("/DISABLE"), ["Painel", "Executor", "Funil", "Varredura diaria"])
        self.assertEqual(self.encerrados, [10, 11])
        self.assertIn("PC congelado", self.saida[-1])

    def test_com_trava_ou_pedido_em_andamento_espera_sem_matar_ninguem(self):
        self.trava_viva("varredura")
        self.assertFalse(congelar_mod.congelar(self.cfg.db, self.saida.append))
        self.assertEqual(len(self.tarefas("/DISABLE")), 4, "as tarefas já ficam desabilitadas")
        self.assertEqual(self.encerrados, [])
        self.assertIn("ESPERE", self.saida[-1])
        tr.vd.ARQ_TRAVA.unlink()
        self.banco.pedir_reanalise(34510, "painel", "ana", tr.RESERVAS)
        self.banco.pegar_proximo_pedido()
        self.assertFalse(congelar_mod.congelar(self.cfg.db, self.saida.append))
        self.assertIn("1 pedido(s) em andamento", self.saida[-1])
        self.assertEqual(self.encerrados, [])

    def test_tarefa_que_nao_desabilita_ou_processo_que_resiste(self):
        self.falhar_tarefa = "Executor"
        self.assertFalse(congelar_mod.congelar(self.cfg.db, self.saida.append))
        self.assertEqual(self.encerrados, [])
        self.falhar_tarefa = None
        self.processos = [[{"pid": 10, "linha": "pythonw painel_servidor.pyw"}],
                          [{"pid": 10, "linha": "pythonw painel_servidor.pyw"}]]
        self.assertFalse(congelar_mod.congelar(self.cfg.db, self.saida.append))
        self.assertIn("ainda rodando", self.saida[-1])

    def test_consulta_de_processos_falha_fechado(self):
        # 2ª confirmação da Turing: erro ou saída estranha do PowerShell NUNCA vira "nenhum processo".
        linha = '"C:\\x\\pythonw.exe" "C:\\x\\painel_servidor.pyw"'
        with mock.patch.object(congelar_mod, "_processos_do_projeto", PROCESSOS_REAIS):
            for resposta in ((1, "Get-CimInstance: acesso negado"), (0, ""), (0, "   "), (0, "{}"),
                             (0, "lixo"), (0, '[{"CommandLine": "x"}]'), (0, '{"ProcessId": 1}')):
                with self.subTest(resposta=resposta):
                    self.respostas_powershell = [resposta]
                    self.saida.clear()
                    self.assertFalse(congelar_mod.congelar(self.cfg.db, self.saida.append))
                    self.assertIn("NÃO está congelado", self.saida[-1])
                    self.assertFalse(any("PC congelado" in t for t in self.saida))
            # a 2ª consulta (depois de encerrar) também falha fechado
            self.respostas_powershell = [(0, json.dumps([{"ProcessId": 10, "CommandLine": linha}])), (1, "erro")]
            self.saida.clear()
            self.assertFalse(congelar_mod.congelar(self.cfg.db, self.saida.append))
            self.assertIn("NÃO está congelado", self.saida[-1])
            # lista vazia de verdade ('[]'): aí sim
            self.respostas_powershell = [(0, "[]"), (0, "[]")]
            self.assertTrue(congelar_mod.congelar(self.cfg.db, self.saida.append))
            self.respostas_powershell = [(0, json.dumps([{"ProcessId": 10, "CommandLine": linha},
                                                         {"ProcessId": 12, "CommandLine": "python outro.py"}]))]
            self.assertEqual(congelar_mod._processos_do_projeto(), [{"pid": 10, "linha": linha}])

    def test_descongelar(self):
        self.assertTrue(congelar_mod.descongelar(saida=self.saida.append))
        self.assertEqual(self.tarefas("/ENABLE"), ["Executor", "Funil", "Varredura diaria"],
                         "o Painel do PC fica desligado depois da virada")
        self.comandos.clear()
        self.assertTrue(congelar_mod.descongelar(com_painel=True, saida=self.saida.append))
        self.assertEqual(self.tarefas("/ENABLE"), ["Executor", "Funil", "Varredura diaria", "Painel"])

    def test_linha_de_comando(self):
        self.trava_viva("funil")
        with mock.patch.dict(os.environ, {"SECRETARIA_PAINEL_DB": str(self.cfg.db),
                                          "SECRETARIA_SAIDAS": str(self.servidor_saidas)}, clear=True), \
                mock.patch("sys.stdout"), self.assertRaises(SystemExit):
            tp.cli.main(["congelar-pc"])


class _Unico:
    def __enter__(self):
        return True

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
