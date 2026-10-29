"""Consumo real da IA (21/10/2026) - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_consumo_ia.py -v

O Gemini é um cliente falso com usage_metadata; os arquivos consumo_ia-AAAA-MM.jsonl
ficam num diretório temporário. Mesma blindagem do test_painel.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import test_cache_ia as tc  # noqa: E402  (blindagem antes do projeto)

import json  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import analisar_documentos as ad  # noqa: E402
import varredura_diaria as vd  # noqa: E402
from painel import consumo  # noqa: E402
from painel import sincronia  # noqa: E402
from painel.app import criar_app  # noqa: E402

tp = tc.tp
PASTA_COM_NOME = "FULANA DE TAL FICTICIA-2o Promissario Comprador-reserva-34659"
USO = types.SimpleNamespace(prompt_token_count=1800, candidates_token_count=250, thoughts_token_count=400,
                            total_token_count=2450)
FLASH = "gemini-2.5-flash"


def custo_flash(entrada, saida, raciocinio):
    return consumo.custo_usd(entrada, saida, raciocinio, consumo.PRECOS_POR_MODELO[FLASH])


class Base(tc.BaseCache):
    def setUp(self):
        super().setUp()
        self.dados = self.tmp / "painel_dados"
        guardados = (ad.PASTA_CONSUMO_IA, ad.ORIGEM_DO_CONSUMO)
        self.addCleanup(lambda: (setattr(ad, "PASTA_CONSUMO_IA", guardados[0]),
                                 setattr(ad, "ORIGEM_DO_CONSUMO", guardados[1])))
        ad.PASTA_CONSUMO_IA = self.dados

    def linhas(self, quando=None):
        return [json.loads(l) for l in consumo.arquivo_do_mes(self.dados, quando or datetime.now())
                .read_text(encoding="utf-8").splitlines()]

    def cliente_falso(self, texto=json.dumps(tc.LEITURA), uso=USO, erro=None):
        def gerar(**k):
            if erro:
                raise erro
            return types.SimpleNamespace(text=texto, candidates=None, usage_metadata=uso)
        return types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gerar))

    def extrair(self, cliente, arquivo, tipo_cv="Comprovante de Endereço"):
        falso = types.SimpleNamespace(Part=types.SimpleNamespace(from_bytes=lambda **k: k),
                                      GenerateContentConfig=lambda **k: k)
        with mock.patch.dict(sys.modules, {"google.genai": types.SimpleNamespace(types=falso),
                                           "google.genai.types": falso}), \
                mock.patch.object(ad, "preparar_conteudo_para_ia", lambda c: [("image/png", b"1"), ("image/png", b"2")]):
            return tc.BaseCache_extrair(cliente, FLASH, arquivo, tipo_cv)


# ==========================================================================
# Registro: uma linha por chamada, sem dado pessoal, nunca quebra a análise
# ==========================================================================
class Registro(Base):
    def test_chamada_grava_o_usage_metadata_sem_dado_pessoal(self):
        pasta = self.saidas / "reserva" / PASTA_COM_NOME
        pasta.mkdir(parents=True)
        arquivo = pasta / "Comprovante - conta de luz FULANA DE TAL 123.456.789-09.pdf"
        arquivo.write_bytes(b"%PDF")
        ad.ORIGEM_DO_CONSUMO = "reanalise"
        self.assertEqual(self.extrair(self.cliente_falso(), arquivo)["nome_encontrado"], "FULANA FICTICIA")
        [linha] = self.linhas()
        self.assertEqual(set(linha), set(consumo.CAMPOS), "lista fechada de campos")
        self.assertEqual((linha["origem"], linha["idreserva"], linha["tipo_cv"], linha["paginas"], linha["modelo"]),
                         ("reanalise", 34659, "Comprovante de Endereço", 2, FLASH))
        self.assertEqual((linha["entrada"], linha["saida"], linha["raciocinio"], linha["total"]),
                         (1800, 250, 400, 2450))
        self.assertEqual((linha["do_cache"], linha["erro"]), (False, False))
        texto = consumo.arquivo_do_mes(self.dados, datetime.now()).read_text(encoding="utf-8")
        for proibido in ("FULANA", "123.456.789", "conta de luz", ".pdf", "Promissario", "LEITURA", "Rua"):
            self.assertNotIn(proibido, texto)

    def test_tipo_cv_so_da_lista_fechada(self):
        # Revisão da Turing: texto livre no tipo poderia carregar dado pessoal.
        casos = (("Comprovante de Endereço", "Comprovante de Endereço"),
                 ("comprovante de endereco ", "Comprovante de Endereço"),
                 ("MO Pesquisa Cadastral ", "MO Pesquisa Cadastral"),
                 ("CERTIDÃO DE ESTADO CIVIL", "Certidão de Estado Civil"),
                 ("Documento da FULANA DE TAL 123.456.789-09", "outro"), ("", "outro"), (None, "outro"))
        for bruto, esperado in casos:
            with self.subTest(bruto=bruto):
                self.assertEqual(consumo.tipo_conhecido(bruto), esperado)
        consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="RG da FULANA 123.456.789-09")
        self.assertEqual(self.linhas()[-1]["tipo_cv"], "outro")
        self.assertNotIn("FULANA", consumo.arquivo_do_mes(self.dados, datetime.now()).read_text(encoding="utf-8"))

    def test_erro_da_ia_tambem_e_registrado(self):
        arquivo = self.tmp / "x-reserva-7" / "a.pdf"
        arquivo.parent.mkdir()
        arquivo.write_bytes(b"x")
        with self.assertRaises(ad.ia_falhas.FalhaIA):  # falha da IA (21/10/2026)
            self.extrair(self.cliente_falso(erro=TimeoutError("demorou")), arquivo)
        with self.assertRaises(ad.ia_falhas.FalhaIA):  # resposta inválida
            self.extrair(self.cliente_falso(texto="nao e json"), arquivo)
        erros = [l["erro"] for l in self.linhas()]
        self.assertEqual(erros, [True, True])
        self.assertEqual(self.linhas()[1]["entrada"], 1800, "a chamada que voltou sem JSON também custou")

    def test_falha_no_registro_nao_quebra_a_analise(self):
        arquivo = self.tmp / "x-reserva-7" / "a.pdf"
        arquivo.parent.mkdir()
        arquivo.write_bytes(b"x")
        ad.PASTA_CONSUMO_IA = arquivo  # um ARQUIVO no lugar da pasta: não dá para gravar
        self.assertEqual(self.extrair(self.cliente_falso(), arquivo)["tipo_identificado"], "Conta de luz")
        with mock.patch.object(consumo.os, "open", side_effect=OSError("disco cheio")):
            self.assertFalse(consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x"))
        ad.PASTA_CONSUMO_IA = self.dados
        with mock.patch.object(ad.consumo_ia, "registrar", side_effect=RuntimeError("bug")):
            self.assertEqual(self.extrair(self.cliente_falso(), arquivo)["tipo_identificado"], "Conta de luz")
            self.processar(self.pasta())  # a análise inteira (com o cache), com o registro quebrado
            self.processar(self.pasta())
        lido = ad.ler_resultados_anteriores(self.saidas / "reserva" / "FULANA-FICTICIA-reserva-777" /
                                            "FULANA-FICTICIA-reserva-777.xlsx")
        self.assertEqual(lido["c.pdf"]["doc_ia"]["nome_encontrado"], "FULANA FICTICIA")

    def test_acerto_do_cache_conta_com_tokens_zero(self):
        self.processar(self.pasta())  # 1ª: a IA (falsa) lê; 2ª: vem do cache
        self.processar(self.pasta())
        [linha] = [l for l in self.linhas() if l["do_cache"]]
        self.assertEqual((linha["entrada"], linha["saida"], linha["raciocinio"], linha["idreserva"]), (0, 0, 0, 777))

    def test_linha_quebrada_e_ignorada(self):
        consumo.registrar(self.dados, origem="xyz", idreserva="abc", tipo_cv="x", uso=USO)
        with open(consumo.arquivo_do_mes(self.dados, datetime.now()), "a", encoding="utf-8") as f:
            f.write("{meia linha\n")
        [linha] = consumo.ler(self.dados, consumo.meses_da_tela())
        self.assertEqual((linha["origem"], linha["idreserva"], linha["tipo_cv"]), ("outro", None, "outro"))


# ==========================================================================
# Escrita: append com laço até a linha inteira, trava entre threads e processos
# ==========================================================================
class Escrita(Base):
    def test_escrita_parcial_continua_ate_o_fim(self):
        original = os.write
        with mock.patch.object(consumo.os, "write", side_effect=lambda fd, b: original(fd, b[:7])):
            self.assertTrue(consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="CTPS", uso=USO))
        [linha] = self.linhas()
        self.assertEqual((linha["tipo_cv"], linha["total"]), ("CTPS", 2450))

    def test_trava_entre_processos(self):
        trava = self.dados / "consumo_ia.lock"
        self.dados.mkdir(parents=True)
        trava.write_text("outro processo")
        with mock.patch.object(consumo, "ESPERA_DA_TRAVA_S", 0.2):
            comeco = time.monotonic()
            self.assertFalse(consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x"))
            self.assertLess(time.monotonic() - comeco, 2, "espera curta: a análise não fica presa")
        self.assertFalse(consumo.arquivo_do_mes(self.dados, datetime.now()).exists())
        velha = time.time() - consumo.TRAVA_VELHA_APOS_S - 5
        os.utime(trava, (velha, velha))  # dono morreu no meio
        self.assertTrue(consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x"))
        self.assertFalse(trava.exists(), "a trava é solta")

    def test_muitas_threads_linhas_inteiras(self):
        def gravar():
            for _ in range(25):
                consumo.registrar(self.dados, origem="varredura", idreserva=34659, tipo_cv="Comprovante de Renda",
                                  uso=USO)
        fios = [threading.Thread(target=gravar) for _ in range(8)]
        [f.start() for f in fios]
        [f.join() for f in fios]
        linhas = consumo.ler(self.dados, consumo.meses_da_tela())
        self.assertEqual(len(linhas), 200, "nenhuma linha perdida nem misturada")


# ==========================================================================
# Retenção: um arquivo por mês, 13 meses; a tela lê só os meses que mostra
# ==========================================================================
class Retencao(Base):
    def test_um_arquivo_por_mes_e_13_guardados(self):
        hoje = datetime(2026, 10, 21, 10)
        for n in range(15, 0, -1):  # 15 meses para trás
            ano, mes = consumo._meses_para_tras(hoje, n)
            self.assertTrue(consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x",
                                              agora=datetime(ano, mes, 5)))
        consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x", agora=hoje)
        nomes = sorted(p.name for p in self.dados.glob("consumo_ia-*.jsonl"))
        self.assertEqual(len(nomes), 13)
        self.assertEqual((nomes[0], nomes[-1]), ("consumo_ia-2025-10.jsonl", "consumo_ia-2026-10.jsonl"))

    def test_a_tela_le_so_o_mes_atual_e_o_anterior(self):
        agora = datetime(2026, 10, 21, 10)
        self.assertEqual(consumo.meses_da_tela(agora), [(2026, 10), (2026, 9)])
        self.assertEqual(consumo.meses_da_tela(datetime(2026, 1, 3)), [(2026, 1), (2025, 12)])
        for quando in (datetime(2026, 10, 1), datetime(2026, 9, 30), datetime(2026, 8, 31)):
            consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x", modelo=FLASH, uso=USO,
                              agora=quando)
        lidas = consumo.ler(self.dados, consumo.meses_da_tela(agora))
        self.assertEqual(sorted(l["em"][:7] for l in lidas), ["2026-09", "2026-10"], "agosto não é lido")


# ==========================================================================
# Preço por modelo e custo
# ==========================================================================
def linha(em, entrada=0, saida=0, raciocinio=0, do_cache=False, idreserva=1, erro=False, modelo=FLASH):
    return {"em": em.strftime("%Y-%m-%d %H:%M:%S"), "_em": em, "origem": "varredura", "idreserva": idreserva,
            "do_cache": do_cache, "erro": erro, "entrada": entrada, "saida": saida, "raciocinio": raciocinio,
            "modelo": modelo}


class Precos(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_tabela_por_modelo(self):
        modelos = consumo.precos()["modelos"]
        self.assertEqual({m: (p["entrada"], p["saida"]) for m, p in modelos.items()},
                         {"gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-flash-lite": (0.10, 0.40),
                          "gemini-2.5-pro": (1.25, 10.00)})
        self.assertTrue(all(p["data"] for p in modelos.values()), "toda linha tem a data do preço")
        s = consumo.somar([linha(datetime(2026, 10, 1), 1_000_000, 500_000, 500_000, modelo="gemini-2.5-pro"),
                           linha(datetime(2026, 10, 1), 1_000_000, 0, 0, modelo="gemini-2.5-flash-lite"),
                           linha(datetime(2026, 10, 1), do_cache=True, idreserva=2, modelo="")])
        self.assertEqual((s["chamadas"], s["do_cache"], s["usd"], s["reservas"]), (2, 1, round(1.25 + 10 + 0.10, 4), 2))
        self.assertEqual(s["brl"], round((1.25 + 10 + 0.10) * 5.50, 2))

    def test_override_por_variavel(self):
        env = {"SECRETARIA_PRECO_GEMINI_2_5_FLASH": "0,15/0.60/01/11/2026", "SECRETARIA_COTACAO_USD_BRL": "abc",
               "SECRETARIA_PRECO_GEMINI_3_FLASH": "0.50/3.00", "SECRETARIA_PRECO_LIXO": "nao/e/numero"}
        with mock.patch.dict(os.environ, env):
            p = consumo.precos()
        flash = p["modelos"][FLASH]
        self.assertEqual((flash["entrada"], flash["saida"], flash["data"]), (0.15, 0.60, "01/11/2026"))
        self.assertIn("variável SECRETARIA_PRECO_GEMINI_2_5_FLASH", flash["origem"])
        self.assertEqual(p["modelos"]["gemini-3-flash"]["entrada"], 0.50)
        self.assertNotIn("lixo", p["modelos"])
        self.assertEqual(p["cotacao_usd_brl"], 5.50)

    def test_modelo_sem_preco_e_desconhecido_nunca_um_preco_errado(self):
        linhas = [linha(datetime(2026, 10, 21, 8), 1_000_000), linha(datetime(2026, 10, 21, 9), 5, modelo="gemini-9")]
        r = consumo.agregar(linhas, datetime(2026, 10, 21, 15))
        for periodo in ("hoje", "sete_dias", "mes_atual"):
            self.assertIsNone(r[periodo]["usd"], periodo)
            self.assertIsNone(r[periodo]["brl_por_documento"])
            self.assertEqual(r[periodo]["modelos_sem_preco"], ["gemini-9"])
        self.assertIsNone(r["projecao_mes"]["usd"])
        self.assertEqual(r["hoje"]["entrada"], 1_000_005, "os tokens continuam somados")
        self.assertEqual(r["mes_anterior"]["usd"], 0.0)

    def test_periodos_e_projecao(self):
        agora = datetime(2026, 10, 21, 15, 0)
        um_milhao = dict(entrada=1_000_000)  # US$ 0,30 cada no flash
        linhas = [linha(datetime(2026, 10, 21, 8), **um_milhao), linha(datetime(2026, 10, 21, 9), do_cache=True),
                  linha(datetime(2026, 10, 18), **um_milhao), linha(datetime(2026, 10, 14, 23, 59), **um_milhao),
                  linha(datetime(2026, 10, 2), **um_milhao), linha(datetime(2026, 9, 30), **um_milhao),
                  linha(datetime(2026, 8, 31), **um_milhao)]
        r = consumo.agregar(linhas, agora)
        self.assertEqual((r["hoje"]["chamadas"], r["hoje"]["do_cache"], r["hoje"]["usd"]), (1, 1, 0.3))
        self.assertEqual(r["sete_dias"]["chamadas"], 2, "15 a 21/10")
        self.assertEqual(r["mes_atual"]["chamadas"], 4)
        self.assertEqual((r["mes_anterior"]["chamadas"], r["mes_anterior"]["usd"]), (1, 0.3))
        self.assertAlmostEqual(r["projecao_mes"]["usd"], round(1.2 + 0.6 / 7 * 10, 2))
        self.assertEqual(r["projecao_mes"]["dias_restantes"], 10)


# ==========================================================================
# A tela (só admin), o envio e a linha da varredura
# ==========================================================================
class Tela(tp.Base):
    def entrar_admin(self, cliente, real=False):
        pagina = cliente.get("/login")
        marca = 'name="csrf" value="' if real else 'name="csrf" content="'
        csrf = pagina.text.split(marca)[1].split('"')[0]
        cliente.post("/login", data={"login": "chefe", "senha": tp.SENHA, "next": "/", "csrf": csrf})

    def test_so_admin_ve(self):
        consumo.registrar(self.cfg.db.parent, origem="varredura", idreserva=1001, tipo_cv="RG", modelo=FLASH, uso=USO)
        consumo.registrar(self.cfg.db.parent, origem="varredura", idreserva=1001, tipo_cv="RG", do_cache=True)
        tp.auth.criar_usuario(self.banco, "chefe", "Chefe", "admin", tp.SENHA)
        self.entrar()  # ana, gerente
        self.assertEqual(self.client.get("/admin/consumo-ia").status_code, 403)
        self.client.post("/logout", data={"csrf": self.client.get("/").text.split('content="')[1].split('"')[0]})
        self.entrar_admin(self.client)
        pagina = self.client.get("/admin/consumo-ia")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("hoje=1/1", pagina.text)
        self.assertIn(f"mes_usd={round(custo_flash(1800, 250, 400), 4)}", pagina.text)
        self.assertIn("flash=0.3/2.5 cotacao=5.5 data=21/10/2026", pagina.text)
        self.assertIn("sem_dados=False", pagina.text)

    def test_tela_de_verdade_com_modelo_sem_preco(self):
        consumo.registrar(self.cfg.db.parent, origem="varredura", idreserva=1001, tipo_cv="RG", modelo="gemini-9",
                          uso=USO)
        app = criar_app(self.cfg)  # templates de verdade
        tp.auth.criar_usuario(app.state.banco, "chefe", "Chefe", "admin", tp.SENHA)
        cliente = TestClient(app, follow_redirects=False)
        self.addCleanup(cliente.close)
        self.entrar_admin(cliente, real=True)
        html = cliente.get("/admin/consumo-ia").text
        for trecho in ("Consumo da IA", "Projeção do mês", "desconhecido", "Modelo sem preço conhecido: gemini-9",
                       "gemini-2.5-flash-lite", "gemini-2.5-pro", "US$ 0.30", "SECRETARIA_COTACAO_USD_BRL"):
            self.assertIn(trecho, html)
        self.assertIn('href="/admin/consumo-ia"', cliente.get("/").text)


class EnvioEVarredura(Base):
    def test_vai_no_envio_pelo_padrao_do_nome_e_e_opcional(self):
        import sincronizar_servidor as ss
        self.assertTrue(sincronia.dado_permitido("consumo_ia-2026-10.jsonl"))
        for ruim in ("consumo_ia.jsonl", "consumo_ia-2026-1.jsonl", "consumo_ia-2026-10.jsonl.bak", "consumo.lock"):
            self.assertFalse(sincronia.dado_permitido(ruim), ruim)
        self.assertEqual(sincronia.destino("painel_dados/consumo_ia-2026-10.jsonl", self.saidas, self.tmp / "d").name,
                         "consumo_ia-2026-10.jsonl")
        self.assertFalse(any(n.startswith("consumo_ia") for n in ss.ARQUIVOS_OBRIGATORIOS))
        originais = (ss.PASTA_SAIDAS, ss.PASTA_DADOS)
        self.addCleanup(lambda: (setattr(ss, "PASTA_SAIDAS", originais[0]), setattr(ss, "PASTA_DADOS", originais[1])))
        ss.PASTA_SAIDAS, ss.PASTA_DADOS = self.saidas, self.dados
        (self.saidas / "reserva").mkdir(parents=True, exist_ok=True)
        consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x", agora=datetime(2026, 9, 3))
        consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x")
        (self.dados / "consumo_ia.lock.bak").write_text("x")
        itens, falhas = ss._arquivos_locais()
        nomes = [c for c, _, _ in itens if "consumo" in c]
        self.assertEqual(nomes, sorted(f"painel_dados/{p.name}" for p in self.dados.glob("consumo_ia-*.jsonl")))
        self.assertTrue(all("reservas_cv" in f or "varreduras" in f for f in falhas), "só os obrigatórios faltam")

    def test_linha_final_da_varredura(self):
        inicio = datetime.now() - timedelta(seconds=2)
        consumo.registrar(self.dados, origem="reanalise", idreserva=1, tipo_cv="x", modelo=FLASH, uso=USO)
        consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x", modelo=FLASH, uso=USO)
        consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x", do_cache=True)
        texto = vd._linha_do_consumo(inicio)
        self.assertIn("IA: 1 leitura(s) + 1 do cache, 2.450 tokens", texto)
        self.assertIn("(1.800 entrada, 250 saída, 400 raciocínio)", texto)
        self.assertIn(f"~US$ {custo_flash(1800, 250, 400):.4f}", texto)
        consumo.registrar(self.dados, origem="varredura", idreserva=1, tipo_cv="x", modelo="gemini-9", uso=USO)
        self.assertIn("custo desconhecido (modelo sem preço: gemini-9)", vd._linha_do_consumo(inicio))
        with mock.patch.object(ad, "PASTA_CONSUMO_IA", self.tmp / "nao-existe"):
            self.assertIn("IA: 0 leitura(s)", vd._linha_do_consumo(inicio))


if __name__ == "__main__":
    unittest.main()
