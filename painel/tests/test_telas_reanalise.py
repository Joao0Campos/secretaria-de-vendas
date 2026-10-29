"""Testes das telas do funil e da reanálise (Parte C, 19/10/2026) - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_telas_reanalise.py -v

Mesma Base e blindagem do test_reanalise/test_painel, mas com os TEMPLATES DE
VERDADE (o test_painel usa templates falsos): é o HTML que a equipe vê.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import test_reanalise as tr  # noqa: E402  (blindagem antes do projeto)

import dataclasses  # noqa: E402
import re  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import varredura_diaria as vd  # noqa: E402
from painel import funil as funil_mod  # noqa: E402
from painel.app import criar_app  # noqa: E402

tp = tr.tp


class BaseTelas(tr.BaseFila):
    def setUp(self):
        super().setUp()
        self.client.close()
        self.app = criar_app(self.cfg)  # templates de verdade
        self.banco = self.app.state.banco
        self.client = TestClient(self.app, follow_redirects=False)
        self.listagem({1001: "Nova Reserva", 34510: "Nova Reserva", 34520: "Reserva Aprovada",
                       34530: "Análise Gerencial", 99: "Venda Validada"})

    def tentar_login(self, login="ana", senha=tp.SENHA, next="/", client=None):
        """O login.html de verdade leva o csrf num input escondido (o falso, num meta)."""
        client = client or self.client
        pagina = client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', pagina.text).group(1)
        return client.post("/login", data={"login": login, "senha": senha, "next": next, "csrf": csrf})

    def listagem(self, situacoes: dict, quando: str = "2026-10-19 12:00:00"):
        vd._gravar_listagem({"atualizado_em": quando, "ultima_referencia": quando, "reservas": {
            str(i): {"idreserva": i, "situacao": s, "tipovenda": "Venda Financiamento"}
            for i, s in situacoes.items()}})

    def html(self, url: str) -> str:
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.text


class Topo(BaseTelas):
    def test_etapas_iguais_as_da_varredura(self):
        self.assertEqual(funil_mod.STATUS_DO_FUNIL, tuple(vd.STATUS_DA_VARREDURA))

    def test_funil_atualizado_de_outro_dia_mostra_a_data(self):
        self.listagem({1001: "Nova Reserva"}, "2026-10-01 12:00:00")
        self.entrar()
        pagina = self.html("/")
        self.assertIn("Funil do CV atualizado às 01/10 12:00 (segundo a listagem do CV)", pagina)
        self.assertIn('class="funil v-alerta"', pagina)

    def test_funil_de_hoje_mostra_a_hora(self):
        self.listagem({1001: "Nova Reserva"}, datetime.now().strftime("%Y-%m-%d 09:45:00"))
        self.entrar()
        for url in ("/", "/reserva/reserva/1001", "/impacto", "/revisao"):
            with self.subTest(url=url):
                pagina = self.html(url)
                self.assertIn("Funil do CV atualizado às 09:45 (segundo a listagem do CV)", pagina)
                self.assertIn('class="funil v-ok"', pagina)

    def test_sem_listagem_nao_mostra_o_funil(self):
        (self.dados / "reservas_cv.json").unlink()
        self.entrar()
        self.assertNotIn("Funil do CV", self.html("/"))

    def test_sem_script_inline(self):
        """A CSP só deixa script do próprio painel: todo <script> tem src."""
        self.entrar()
        for url in ("/", "/reserva/reserva/1001"):
            scripts = re.findall(r"<script([^>]*)>", self.html(url))
            self.assertTrue(scripts)
            self.assertTrue(all('src="/static/' in s for s in scripts), scripts)

    def test_js_consulta_a_fila_a_cada_5_s(self):
        self.entrar()
        js = self.client.get("/static/painel.js").text
        for trecho in ('"/api/reanalise"', '"/api/reanalise/" + caixa.dataset.reanalise', "5000", "window.location.reload()",
                       '"X-CSRF-Token": csrf'):
            self.assertIn(trecho, js)


class Lista(BaseTelas):
    def test_no_funil_sem_analise_com_botao(self):
        self.entrar()
        pagina = self.html("/")
        self.assertIn("No funil do CV, ainda sem análise (3)", pagina)
        self.assertIn('<details class="card grupo" open data-lembrar="no-funil-sem-analise">', pagina,
                      "a lista recolhe com um clique no título")
        for i in (34510, 34520, 34530):
            self.assertIn(f'data-reanalise="{i}"', pagina)
        self.assertEqual(pagina.count("Analisar agora"), 3)
        self.assertNotIn('data-reanalise="99"', pagina, "fora do funil não entra")
        self.assertNotIn('data-reanalise="1001"', pagina, "já analisada não entra")
        ids = [s["id"] for s in self.client.get("/api/reservas").json()["no_funil_sem_analise"]]
        self.assertEqual(ids, [34530, 34520, 34510])
        self.assertEqual(self.client.get("/api/reservas").json()["no_funil_sem_analise"][0],
                         {"id": 34530, "situacao": "Análise Gerencial", "modalidade": "Venda Financiamento"})

    def test_menu_impacto_oculto_admin_so_para_admin(self):
        self.client.close()
        self.app = criar_app(dataclasses.replace(self.cfg, mostrar_impacto=False))
        self.client = TestClient(self.app, follow_redirects=False)
        tp.auth.criar_usuario(self.app.state.banco, "chefe", "Chefe", "admin", tp.SENHA)
        self.entrar()
        pagina = self.html("/")
        self.assertNotIn('href="/impacto"', pagina)
        self.assertNotIn('href="/admin/usuarios"', pagina)
        self.assertIn('href="/trocar-senha"', pagina)
        self.assertIn('name="atual"', self.html("/trocar-senha"))
        self.client.post("/logout", data={"csrf": re.search(r'name="csrf" content="([^"]+)"', pagina).group(1)})
        self.assertEqual(self.tentar_login(login="chefe").status_code, 303)
        pagina = self.html("/admin/usuarios")
        self.assertIn('action="/admin/usuarios"', pagina)
        self.assertIn('href="/admin/usuarios"', pagina)
        self.assertNotIn("<script>", pagina.replace('<script src="/static/painel.js" defer></script>', ""))
        self.assertNotIn("onclick", pagina)

    def test_pre_cadastro_saiu_do_site(self):
        self.entrar()
        pagina = self.html("/")
        for trecho in ("Pré-cadastro", "pre_cadastro", "tipo=", ">PC<"):
            self.assertNotIn(trecho, pagina)
        self.assertEqual(self.client.get("/reserva/pre_cadastro/2002").status_code, 404)

    def test_analisada_que_saiu_do_funil_e_marcada(self):
        self.listagem({1001: "Venda Validada"})
        self.entrar()
        self.assertIn("saiu do funil: agora em Venda Validada", self.html("/"))
        item = next(i for i in self.client.get("/api/reservas").json()["itens"] if i["id"] == 1001)
        self.assertEqual((item["saiu_do_funil"], item["situacao_listagem"]), ("Venda Validada", "Venda Validada"))

    def test_analisada_no_funil_nao_e_marcada(self):
        self.entrar()
        self.assertNotIn("saiu do funil", self.html("/"))


class PaginaDaReserva(BaseTelas):
    def test_botao_reanalisar(self):
        self.entrar()
        pagina = self.html("/reserva/reserva/1001")
        self.assertIn('data-reanalise="1001"', pagina)
        self.assertIn("Reanalisar agora", pagina)

    def test_apontamento_com_veredito_guiado_e_como_conferir(self):
        self.entrar()
        pagina = self.html("/reserva/reserva/1001")
        self.assertIn("Analisar este apontamento", pagina)
        self.assertIn("Este apontamento é um problema real nesta reserva?", pagina)
        self.assertIn("Como conferir este aviso", pagina)
        self.assertIn('<details class="como-conferir">', pagina)
        for parte in ("O que é:", "Onde olhar:", "Como decidir:", "O que fazer:"):
            self.assertIn(parte, pagina)
        self.assertNotIn("Marcar corrigido", pagina)
        self.assertNotIn("Ignorar…", pagina)

    def test_veredito_aparece_depois_de_salvo(self):
        csrf = self.entrar()
        chave = re.search(r'data-chave="([^"]+)"', self.html("/reserva/reserva/1001")).group(1)
        r = self.client.post("/api/tratativa", headers={"X-CSRF-Token": csrf}, json={
            "chave": chave, "status": "aberto", "veredito": "nao_sei", "detalhe": "foto cortada"})
        self.assertEqual(r.status_code, 200, r.text)
        pagina = self.html("/reserva/reserva/1001")
        self.assertIn("Dúvida", pagina)
        self.assertIn("foto cortada", pagina)
        self.assertIn("Analisar este apontamento", pagina, "aberto com dúvida continua analisável")

    def test_catalogo_cobre_todas_as_categorias(self):
        from painel.como_conferir import GENERICO, PARTES, VERBETES, como_conferir
        from testar_amostra import CATEGORIAS
        for rotulo, _ in CATEGORIAS:
            self.assertIn(rotulo, VERBETES, rotulo)
        for rotulo, verbete in list(VERBETES.items()) + [("Outros", GENERICO)]:
            for chave, _ in PARTES:
                self.assertTrue(verbete.get(chave), (rotulo, chave))
        self.assertEqual(como_conferir("Outros"), GENERICO)
        self.assertEqual(como_conferir("categoria que não existe"), GENERICO)
        self.assertEqual(como_conferir(None), GENERICO)

    def test_js_do_veredito_guiado(self):
        self.entrar()
        js = self.client.get("/static/painel.js").text
        for trecho in ('"data-acao": "analisar"', "Analisar este apontamento", "form.veredito",
                       'input[name="veredito"]:checked', "ROTULO_VEREDITO", "veredito,",
                       '"Corrigido fora do CV"', '"Falso positivo da IA"',
                       '"Regra não se aplica a este caso"', 'MOTIVO_OUTRO + texto', "(obrigatório)"):
            self.assertIn(trecho, js)
        self.assertNotIn("Marcar corrigido", js)

    def test_sem_botao_se_a_reserva_nao_esta_na_listagem(self):
        self.listagem({34510: "Nova Reserva"})
        self.entrar()
        self.assertNotIn("Reanalisar agora", self.html("/reserva/reserva/1001"))

    def test_saiu_do_funil_na_pagina(self):
        self.listagem({1001: "Distrato"})
        self.entrar()
        self.assertIn("saiu do funil: agora em Distrato", self.html("/reserva/reserva/1001"))

    def test_fora_do_funil_sem_botao(self):
        self.listagem({1001: "Venda Validada"})
        self.entrar()
        pagina = self.html("/reserva/reserva/1001")
        self.assertIn("saiu do funil: agora em Venda Validada", pagina)
        self.assertNotIn("Reanalisar agora", pagina)
        self.assertNotIn('data-reanalise="1001"', pagina)
        self.assertFalse(self.reserva()["pode_reanalisar"])
        item = next(i for i in self.client.get("/api/reservas").json()["itens"] if i["id"] == 1001)
        self.assertFalse(item["pode_reanalisar"])

    def test_reanalise_aparece_sem_reiniciar_o_site(self):
        """O executor regrava a planilha; o MESMO app, sem reiniciar, já mostra
        a análise nova (repo.atualizar() relê pelo mtime)."""
        csrf = self.entrar()
        antes = self.reserva()
        self.assertTrue(any("FULANA OUTRA" in a["texto"] for p in antes["pessoas"]
                            for d in p["documentos"] for a in d["apontamentos"]))

        def reanalise_regrava(pasta, **k):
            if "FULANA" in str(pasta):
                tp.regravar_divergencias(self.planilhas["titular"], [
                    tp.resultado("rg.pdf", "RG, CPF, CNH ou OUTROS", "RG"),
                    tp.resultado("comprovante.pdf", "Comprovante de Endereço", "Conta de luz")])
        vd.processar_reserva_completa = lambda idr, devolver_associados=False: (
            [self.planilhas["titular"].parent], [], None)
        vd.processar_pasta_cliente = reanalise_regrava
        self.assertEqual(self.pedir(1001, csrf).status_code, 200)
        self.assertEqual(self.executor().atender_um(), "concluido")
        depois = self.reserva()
        self.assertFalse(any("FULANA OUTRA" in a["texto"] for p in depois["pessoas"]
                             for d in p["documentos"] for a in d["apontamentos"]))
        estado = self.client.get("/api/reanalise/1001").json()
        self.assertEqual(estado["pedido"]["status"], "concluido")


if __name__ == "__main__":
    unittest.main()
