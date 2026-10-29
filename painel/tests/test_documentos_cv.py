"""Erro 2 de 20/10/2026: o CV não entrega os documentos de 34441/34442 - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_documentos_cv.py -v

As respostas do CV são as reais, vistas com GET em 20/10: 422 com corpo
{"error":"Ocorreu um erro inesperado...","code":422} em /reservas/{id}/documentos,
e 200 text/html "Arquivo não encontrado." em /precadastro/34464/documentos e em
/reservas/34440/documentos. requests.get é substituído; a blindagem bloqueia o resto.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import test_funil as tf  # noqa: E402  (blindagem antes do projeto)

import json  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime  # noqa: E402
from unittest import mock  # noqa: E402

import mvp_v2_reserva as mv  # noqa: E402
import varredura_diaria as vd  # noqa: E402

CFG = {"dominio": "exemplo", "email": "x@exemplo", "token": "t"}
CORPO_422 = '{"error":"Ocorreu um erro inesperado, por favor tente mais tarde.","code":422}'


class Resp:
    def __init__(self, status, texto, tipo="application/json"):
        self.status_code, self.text, self.headers = status, texto, {"content-type": tipo}
        self.content = texto.encode()

    def json(self):
        return json.loads(self.text)


def cv_falso(respostas: dict):
    """requests.get falso: a resposta pelo fim da URL."""
    pedidos = []

    def get(url, **k):
        pedidos.append(url)
        for fim, resp in respostas.items():
            if url.endswith(fim):
                return resp
        raise AssertionError(f"URL inesperada: {url}")
    return get, pedidos


REGISTRO_34441 = Resp(200, json.dumps({"34441": {"idprecadastro": 34464, "tipovenda": "Venda Financiamento",
                                                 "situacao": {"situacao": "Análise Gerencial"}}}))
SEM_ARQUIVO = Resp(200, "Arquivo não encontrado.", "text/html; charset=utf-8")


class BuscarDocumentos(unittest.TestCase):
    def buscar(self, respostas, idreserva=34441):
        get, pedidos = cv_falso(respostas)
        with mock.patch.object(mv.requests, "get", get):
            try:
                mv.buscar_documentos_reserva(CFG, idreserva)
            except SystemExit as exc:
                return exc, pedidos
        self.fail("deveria falhar")

    def test_422_com_arquivo_ausente_no_precadastro(self):
        exc, pedidos = self.buscar({"/reservas/34441/documentos": Resp(422, CORPO_422),
                                    "/reservas/34441": REGISTRO_34441,
                                    "/precadastro/34464/documentos": SEM_ARQUIVO})
        self.assertIsInstance(exc, mv.DocumentosIndisponiveisNoCV)
        self.assertTrue(exc.permanente)
        self.assertIn("arquivo ausente no CV, pré-cadastro 34464", exc.motivo)
        self.assertNotIn(":", exc.motivo, "o motivo vai inteiro para o painel (lá corta no ':')")
        self.assertEqual((exc.status, exc.corpo), (422, CORPO_422))
        self.assertTrue(all("/reservas/" in u or "/precadastro/" in u for u in pedidos), "só GET de leitura")

    def test_422_sem_confirmacao_nao_e_permanente(self):
        for nome, respostas in (
                ("registro fora", {"/reservas/34441": Resp(500, "fora")}),
                ("pré-cadastro responde normal", {"/reservas/34441": REGISTRO_34441,
                                                  "/precadastro/34464/documentos": Resp(200, '{"dados": {}}')}),
                ("pré-cadastro também 422", {"/reservas/34441": REGISTRO_34441,
                                             "/precadastro/34464/documentos": Resp(422, CORPO_422)})):
            with self.subTest(nome):
                exc, _ = self.buscar({"/reservas/34441/documentos": Resp(422, CORPO_422), **respostas})
                self.assertIsInstance(exc, mv.DocumentosIndisponiveisNoCV)
                self.assertFalse(exc.permanente, "um 'tente mais tarde' passageiro segue com repescagem")
                self.assertEqual(exc.motivo, "CV não entrega os documentos (HTTP 422, erro do lado do CV)")

    def test_200_sem_json_sem_confirmacao_nao_e_permanente(self):
        for corpo in ("", "<html>manutenção, volte em instantes</html>", "Arquivo não encontrado."):
            with self.subTest(corpo=corpo):
                exc, _ = self.buscar({"/reservas/34441/documentos": Resp(200, corpo, "text/html"),
                                      "/reservas/34441": REGISTRO_34441,
                                      "/precadastro/34464/documentos": Resp(200, '{"dados": {}}')})
                self.assertFalse(exc.permanente)

    def test_200_sem_json_arquivo_nao_encontrado(self):
        exc, _ = self.buscar({"/reservas/34440/documentos": SEM_ARQUIVO,
                              "/reservas/34440": Resp(200, json.dumps({"34440": {"idprecadastro": 34464}})),
                              "/precadastro/34464/documentos": SEM_ARQUIVO}, 34440)
        self.assertIsInstance(exc, mv.DocumentosIndisponiveisNoCV)
        self.assertEqual(exc.status, 200)
        self.assertTrue(exc.permanente)

    def test_outros_erros_seguem_como_antes(self):
        exc, _ = self.buscar({"/reservas/34441/documentos": Resp(500, "erro interno")})
        self.assertNotIsInstance(exc, mv.DocumentosIndisponiveisNoCV)
        self.assertTrue(str(exc).startswith("Erro 500 ao buscar documentos da reserva 34441"))


class Varredura(tf.Base):
    """A falha permanente fica registrada com o motivo e não é repetida no dia."""

    def setUp(self):
        super().setUp()
        vd.ARQ_ULTIMA_OK.unlink()  # dia ainda não feito
        self.motivo = "CV não entrega os documentos - arquivo ausente no CV, pré-cadastro 34464"

        def reserva(idr, devolver_associados=False):
            self.chamadas["reservas"].append(idr)
            if idr == 3:
                raise mv.DocumentosIndisponiveisNoCV(3, 422, CORPO_422, self.motivo, permanente=True)
            return [], [], None
        vd.processar_reserva_completa = reserva

    def test_422_sem_confirmacao_vai_para_a_repescagem(self):
        motivo = "CV não entrega os documentos (HTTP 422, erro do lado do CV)"

        def reserva(idr, devolver_associados=False):
            self.chamadas["reservas"].append(idr)
            raise mv.DocumentosIndisponiveisNoCV(idr, 422, CORPO_422, motivo)
        vd.processar_reserva_completa = reserva
        self.quieto(vd.rodar)
        pendentes = json.loads(vd.ARQ_ULTIMA_OK.read_text(encoding="utf-8"))["pendentes"]
        self.assertEqual(pendentes["3"], {"tentativas": 1, "erro": motivo})
        self.chamadas["reservas"].clear()
        self.quieto(vd.rodar)
        self.assertEqual(sorted(self.chamadas["reservas"]), [1, 3], "repescagem tenta de novo")

    def test_registra_o_motivo_e_a_repescagem_nao_repete(self):
        codigo, saida = self.quieto(vd.rodar)
        self.assertEqual((codigo, sorted(self.chamadas["reservas"])), (0, [1, 3]))
        rodada = json.loads(vd.ARQ_REGISTRO.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rodada["falhas"], [{"idreserva": 3, "erro": self.motivo, "permanente": True}])
        marcador = json.loads(vd.ARQ_ULTIMA_OK.read_text(encoding="utf-8"))
        self.assertEqual(marcador["pendentes"]["3"]["tentativas"], vd.TENTATIVAS_POR_DIA)
        self.chamadas["reservas"].clear()
        codigo, saida = self.quieto(vd.rodar)  # o disparo do logon, no mesmo dia
        self.assertEqual((codigo, self.chamadas["reservas"]), (0, []))
        self.assertIn("já rodou hoje", saida)

    def test_falha_comum_que_vira_permanente_na_repescagem_para(self):
        vd._gravar_marcador({"concluida_em": datetime.now().strftime("%Y-%m-%d 07:29:29"),
                             "reservas_alvo": 2, "reservas_ok": 1, "pastas": 1,
                             "pendentes": {"3": {"tentativas": 1, "erro": "Erro 500 ao buscar documentos"}}})
        self.quieto(vd.rodar)
        self.assertEqual(self.chamadas["reservas"], [3])
        pendente = json.loads(vd.ARQ_ULTIMA_OK.read_text(encoding="utf-8"))["pendentes"]["3"]
        self.assertEqual(pendente, {"tentativas": vd.TENTATIVAS_POR_DIA, "erro": self.motivo, "permanente": True})
        rodada = json.loads(vd.ARQ_REGISTRO.read_text(encoding="utf-8").splitlines()[-1])
        self.assertTrue(rodada["falhas"][0]["permanente"])
        self.quieto(vd.rodar)
        self.assertEqual(self.chamadas["reservas"], [3], "não tenta a 3ª vez")

    def test_falha_comum_continua_com_repescagem(self):
        vd.processar_reserva_completa = lambda idr, devolver_associados=False: (
            self.chamadas["reservas"].append(idr), (_ for _ in ()).throw(SystemExit("Erro 500 ao buscar: x")))[1]
        self.quieto(vd.rodar)
        marcador = json.loads(vd.ARQ_ULTIMA_OK.read_text(encoding="utf-8"))
        self.assertEqual(marcador["pendentes"]["3"], {"tentativas": 1, "erro": "Erro 500 ao buscar"})


if __name__ == "__main__":
    unittest.main()
