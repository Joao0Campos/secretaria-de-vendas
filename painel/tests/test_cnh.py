"""Erro 3 de 20/10/2026: na CNH, o Nº de Registro (ou o RG) lido como CPF - offline.

Rodar (da pasta do projeto ou de qualquer lugar):
    <python do .venv do projeto> -B painel/tests/test_cnh.py -v

Só as regras (comparar_documento_com_cadastro): nenhuma IA, nenhum CV. CPFs e
registros são fictícios, gerados com os dígitos verificadores.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
if str(_AQUI) not in sys.path:
    sys.path.insert(0, str(_AQUI))

import test_painel as tp  # noqa: E402,F401  (blindagem antes do projeto)

import unittest  # noqa: E402

import analisar_documentos as ad  # noqa: E402
import testar_amostra  # noqa: E402


def cpf_com_dv(base9: str) -> str:
    d1 = (sum(int(base9[i]) * (10 - i) for i in range(9)) * 10 % 11) % 10
    d2 = (sum(int((base9 + str(d1))[i]) * (11 - i) for i in range(10)) * 10 % 11) % 10
    return f"{base9}{d1}{d2}"


def registro_cnh_valido(n: str) -> bool:
    """Dígitos verificadores do Nº de Registro da CNH (conferido em 20/10/2026
    contra os registros citados nas leituras do cache: 8 de 9)."""
    if len(n) != 11 or n == n[0] * 11:
        return False
    d1, desconto = sum(int(n[i]) * p for i, p in zip(range(9), range(9, 0, -1))) % 11, 0
    if d1 >= 10:
        d1, desconto = 0, 2
    resto = sum(int(n[i]) * p for i, p in zip(range(9), range(1, 10))) % 11
    return f"{d1}{0 if resto >= 10 else resto - desconto}" == n[-2:]


def registro_com_dv(base9: str) -> str:
    return next(base9 + f"{d:02d}" for d in range(100) if registro_cnh_valido(base9 + f"{d:02d}"))


CPF_CADASTRO = cpf_com_dv("123456789")
CPF_DE_OUTRA_PESSOA = cpf_com_dv("987654321")
REGISTRO = registro_com_dv("048213579")
CADASTRO = {"nome": "FULANA FICTICIA", "documento": CPF_CADASTRO}


def leitura(cpf, tipo="CNH", **extra):
    return dict({"tipo_identificado": tipo, "nome_encontrado": "FULANA FICTICIA", "cpf_encontrado": cpf,
                 "legivel": True, "confianca_campos": {}}, **extra)


def mensagens_de_cpf(doc_ia, tipo_cv="RG, CPF, CNH ou outros"):
    r = ad.comparar_documento_com_cadastro(doc_ia, CADASTRO, tipo_cv, None)
    sobre_cpf = ("CPF no documento", "Número lido na CNH")
    return ([("divergencia", m) for m in r["divergencias"] if m.startswith(sobre_cpf)]
            + [("aviso", m) for m in r["avisos"] if m.startswith(sobre_cpf)])


def niveis(doc_ia):
    return [n for n, _ in mensagens_de_cpf(doc_ia)]


class RegistroDaCnh(unittest.TestCase):
    def test_dados_ficticios(self):
        self.assertTrue(ad.cpf_valido(CPF_CADASTRO) and ad.cpf_valido(CPF_DE_OUTRA_PESSOA))
        self.assertTrue(registro_cnh_valido(REGISTRO))
        self.assertFalse(ad.cpf_valido(REGISTRO))

    def test_so_some_quando_e_o_registro_identificado_pela_ia(self):
        self.assertEqual(mensagens_de_cpf(leitura(REGISTRO, registro_cnh=REGISTRO)), [])
        self.assertEqual(mensagens_de_cpf(leitura(REGISTRO, tipo="Carteira Nacional de Habilitação",
                                                  registro_cnh=REGISTRO)), [])

    def test_numero_invalido_e_diferente_vira_aviso_claro(self):
        # Sem o registro identificado (leituras antigas), ou com outro registro:
        # pode ser a CNH de outra pessoa com o CPF mal lido - nunca some.
        for doc_ia in (leitura(REGISTRO), leitura("660139364"), leitura(REGISTRO, registro_cnh="9" + REGISTRO[1:])):
            with self.subTest(doc_ia=doc_ia):
                msgs = mensagens_de_cpf(doc_ia)
                self.assertEqual([n for n, _ in msgs], ["aviso"], "aviso, nunca divergência")
                texto = msgs[0][1]
                self.assertIn("não é um CPF válido (pode ser o Nº de registro ou o RG) - conferir", texto)
                self.assertEqual(testar_amostra.categorizar(texto), "Número da CNH lido como CPF")

    def test_cpf_lido_com_um_digito_trocado_continua_aviso(self):
        trocado = CPF_CADASTRO[:-1] + str((int(CPF_CADASTRO[-1]) + 1) % 10)  # DV não bate
        self.assertFalse(ad.cpf_valido(trocado))
        self.assertTrue(registro_cnh_valido(trocado), "por acaso também passa como registro")
        msgs = mensagens_de_cpf(leitura(trocado))
        self.assertEqual([n for n, _ in msgs], ["aviso"])
        self.assertEqual(testar_amostra.categorizar(msgs[0][1]), "CPF diferente do cadastro", "texto de sempre")

    def test_cpf_parcial_continua_aviso(self):
        self.assertEqual(niveis(leitura(CPF_CADASTRO[:9])), ["aviso"])

    def test_cpf_valido_de_outra_pessoa_continua_divergencia(self):
        self.assertEqual([n for n, _ in mensagens_de_cpf(leitura(CPF_DE_OUTRA_PESSOA))], ["divergencia"])
        self.assertEqual([n for n, _ in mensagens_de_cpf(
            leitura(CPF_DE_OUTRA_PESSOA, registro_cnh=REGISTRO))], ["divergencia"])

    def test_fora_da_cnh_nada_muda(self):
        for doc_ia in (leitura(REGISTRO, tipo="RG"), leitura("660139364", tipo="RG"),
                       leitura(REGISTRO, tipo="RG", registro_cnh=REGISTRO)):
            with self.subTest(doc_ia=doc_ia):
                msgs = mensagens_de_cpf(doc_ia)
                self.assertEqual([n for n, _ in msgs], ["aviso"])
                self.assertEqual(testar_amostra.categorizar(msgs[0][1]), "CPF diferente do cadastro")

    def test_prompt_separa_cpf_e_registro(self):
        self.assertIn('"registro_cnh"', ad.PROMPT_EXTRACAO)
        self.assertIn("nunca o Nº REGISTRO", ad.PROMPT_EXTRACAO)
        ad.PROMPT_EXTRACAO.format(nome_arquivo="a.pdf", tipo_cv="CNH")  # as chaves continuam escapadas


if __name__ == "__main__":
    unittest.main()
