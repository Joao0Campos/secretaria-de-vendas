"""
MVP v1.1 - Leitura de documentos com IA + confronto com o cadastro
===================================================================

Roda DEPOIS do mvp_v1_validador.py. Pega a pasta que ele criou
(saidas/{nome}-{idprecadastro}/), abre cada documento baixado com a IA
do Google (Gemini, com visão de imagem/PDF), extrai o que dá pra ler
(nome, CPF, data de emissão, endereço, legibilidade) e compara com o que
está na aba "Dados Cadastrais" do Excel gerado pelo script anterior.

O resultado vira uma aba nova "Divergencias (IA)" dentro do MESMO Excel.

O que isso resolve (que a API do CV sozinha não resolve):
  - Comprovante de endereço vencido (mês M vale até o último dia de M+3)
  - Documento ilegível/borrado
  - Nome ou CPF do documento não bate com o nome/documento do cadastro

Configuração extra (além do que o mvp_v1_validador.py já usa):
  - Uma chave de API do Google AI (Gemini) - crie/gerencie em
    https://aistudio.google.com/app/apikey
    Adicione no config.json: "gemini_api_key": "SUA_CHAVE"
    ou defina a variável de ambiente GEMINI_API_KEY (ou GOOGLE_API_KEY).
  - Opcional: "modelo_ia" no config.json pra trocar o modelo usado
    (por padrão usa um modelo com visão do Gemini - confira o nome
    atual em https://ai.google.dev/gemini-api/docs/models).

Dependências novas (rodar uma vez, com o .venv ativo):
    pip install google-genai pymupdf rapidfuzz

IMPORTANTE - custo: cada documento analisado é uma chamada de API paga
(cobrada pelo Google, por token de imagem + texto). Teste primeiro
com uma pasta só antes de rodar em lote pra ter noção do custo.

Como rodar:
    python analisar_documentos.py                  (pergunta no terminal)
    python analisar_documentos.py "FULANO DE TAL-12345"
    python analisar_documentos.py --todas          (roda em todas as pastas de saidas/)
    python analisar_documentos.py --todas --forcar (reprocessa TUDO de novo, ignorando o cache)

Lock/cache: por padrão, um documento que já apareceu numa rodada anterior
da aba "Divergencias (IA)" NÃO é lido de novo pela IA (economiza chamada de
API paga). Só documentos novos (baixados depois da última análise) entram
na chamada. Use --forcar se quiser reprocessar tudo do zero mesmo assim
(ex: depois de mudar as regras de comparação).

Paralelismo: os documentos pendentes de uma pasta são lidos em paralelo
(várias chamadas de IA ao mesmo tempo, em threads - cada chamada é I/O,
então isso é seguro e acelera bastante). Por padrão usa 5 ao mesmo tempo;
troque com "paralelismo_ia" no config.json se sua chave tiver um limite de
requisições por minuto diferente (ex: chave grátis do Gemini costuma ter
limite mais baixo que uma chave paga).
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import os
import secrets
import time
import mimetypes
import re
import sys
import unicodedata
import threading
from datetime import date, datetime
from pathlib import Path

from painel import consumo as consumo_ia  # noqa: E402  (só biblioteca padrão)
from painel import ia_falhas  # noqa: E402  (só biblioteca padrão)
from mvp_v1_validador import (  # reaproveita o que já existe
    buscar_precadastro,
    PASTA_SCRIPT,
    PASTA_SAIDAS,
    PASTA_SAIDAS_PRECADASTRO,
    PASTA_SAIDAS_RESERVA,
    carregar_config,
)
from mvp_v2_reserva import (
    listar_todas_reservas,
    buscar_reserva_individual,
    procurar_reserva_na_listagem,
    buscar_associados_da_reserva,
)

# Particionamento (29/08/2026): saidas/ agora tem duas subpastas -
# pre_cadastro/ (mvp_v1_validador.py) e reserva/ (mvp_v2_reserva.py).
# listar_pastas_de_cliente() varre as duas, e por retrocompatibilidade
# também inclui pastas de cliente soltas direto em saidas/ (geradas antes
# do particionamento existir).
def listar_pastas_de_cliente() -> list[Path]:
    pastas: list[Path] = []
    for base in (PASTA_SAIDAS_PRECADASTRO, PASTA_SAIDAS_RESERVA):
        if base.is_dir():
            pastas.extend(p for p in base.iterdir() if p.is_dir())
    # Retrocompatibilidade: pastas antigas, geradas antes do
    # particionamento, que ficaram soltas direto em saidas/.
    if PASTA_SAIDAS.is_dir():
        pastas.extend(
            p for p in PASTA_SAIDAS.iterdir()
            if p.is_dir() and p not in (PASTA_SAIDAS_PRECADASTRO, PASTA_SAIDAS_RESERVA)
        )
    return pastas


def listar_pastas_de_reserva() -> list[Path]:
    """Só saidas/reserva/ - o que o --todas e o --reler-telas percorrem desde
    20/10/2026 (decisão do João: o projeto não trata mais pré-cadastro; as
    pastas de saidas/pre_cadastro/ são antigas e ficam como estão)."""
    if not PASTA_SAIDAS_RESERVA.is_dir():
        return []
    return sorted(p for p in PASTA_SAIDAS_RESERVA.iterdir() if p.is_dir())


def extrair_idprecadastro_da_pasta(nome_pasta: str) -> int | None:
    """Pasta é nomeada '{nome do cliente}-{idprecadastro}' (pré-cadastro) ou
    '{nome do cliente}-reserva-{idreserva}' (reserva). Extrai o número do
    final em qualquer um dos dois formatos - usado pra buscar a modalidade
    da venda via GET /v1/cvdw/reservas.

    O número devolvido é um idprecadastro OU um idreserva, conforme o formato,
    e os dois NÃO são intercambiáveis: são numerações diferentes na mesma faixa,
    e o mesmo número no outro campo é o registro de outra pessoa (armadilha 5;
    correção de 09/10/2026 em buscar_registro_da_reserva). Quem usa o número
    tem que perguntar antes a numero_da_pasta_e_idreserva() qual dos dois ele é.
    Retorna None se o nome da pasta não seguir nenhum dos dois padrões."""
    m = re.search(r"-(?:reserva-)?(\d+)$", nome_pasta)
    return int(m.group(1)) if m else None


def encontrar_pasta_de_cliente(nome_pasta: str) -> Path | None:
    """Acha uma pasta de cliente pelo nome exato, procurando nas duas
    subpastas (pre_cadastro/, reserva/) e na raiz de saidas/ (legado)."""
    for base in (PASTA_SAIDAS_PRECADASTRO, PASTA_SAIDAS_RESERVA, PASTA_SAIDAS):
        candidata = base / nome_pasta
        if candidata.is_dir():
            return candidata
    return None

MODELO_IA_PADRAO = "gemini-2.5-flash"
PARALELISMO_PADRAO = 5
# Tempo máximo de UMA chamada à IA (google-genai: HttpOptions.timeout, em ms).
# Sem ele o SDK espera para sempre (revisão da Forja, 19/10/2026): uma chamada
# pendurada prendia o executor de reanálise e a trava, e a varredura das 7h
# desistia depois de 15 min. 120 s cobrem com folga um documento de até
# MAX_PAGINAS_PDF_IA páginas. Estourou = falha DAQUELE documento ("Falha ao
# chamar a IA"), como qualquer erro da IA; a reserva segue.
TIMEOUT_IA_MS = 120_000


def criar_cliente_ia(chave: str):
    from google import genai
    from google.genai import types
    return genai.Client(api_key=chave, http_options=types.HttpOptions(timeout=TIMEOUT_IA_MS))

# Tipos de documento que NÃO fazem parte do escopo de validação do contrato
# (conforme "REPROVAÇÃO CONTRATOS": são usados só pelo Repasse, não pela
# validação). Esses documentos são pulados por completo - nem chamam a IA
# (economiza custo) nem podem gerar divergência. Comparação é por trecho
# (case-insensitive) no nome do tipo cadastrado no CV.
TIPOS_FORA_DO_ESCOPO_DE_VALIDACAO = [
    "ctps",  # carteira de trabalho - não é documento de identificação válido pra validação
    # O contrato assinado NÃO é documento de validação - é o RESULTADO do
    # processo, não insumo dele. Além disso vem sempre como .zip do Clicksign,
    # que a leitura por IA não abre, então gerava "não foi possível ler o
    # documento" em TODA reserva com contrato assinado - ou seja, em toda Venda
    # Validada, por construção. Era um dos motivos de 100% dos casos testados
    # aparecerem com apontamento (22/09/2026).
    "contrato de compra e venda",
    # A Simulação CCA não é usada pelo setor de contratos (gestora comercial, 14/10/2026).
    # Era lida pela IA em 43 das 126 pastas de saidas/ só para virar aviso. O
    # termo é o nome inteiro do campo do CV: "simulação" sozinho pegaria também
    # a "Simulação Inicial (Comercial VIC)", que é outro campo e fica como está.
    "simulação cca",
    "simulacao cca",
]


# Documentos que NÃO têm prazo de validade - a regra de "vencido" nunca se
# aplica a eles. Confirmado pelo João em 16/09/2026: a certidão de estado civil
# é como uma certidão de nascimento, vale pra sempre. A cartilha da Milla
# realmente não menciona prazo pra ela (o silêncio era intencional, não
# esquecimento).
TIPOS_SEM_PRAZO_DE_VALIDADE = [
    "certidão de estado civil",
    "certidao de estado civil",
    "certidão de nascimento",
    "certidao de nascimento",
    "certidão de casamento",
    "certidao de casamento",
]


def fora_do_escopo_de_validacao(tipo_cv: str) -> bool:
    tipo_lower = (tipo_cv or "").lower()
    return any(termo in tipo_lower for termo in TIPOS_FORA_DO_ESCOPO_DE_VALIDACAO)


def registro_fora_do_escopo(tipo_cv: str) -> dict:
    """A 'leitura' gravada no lugar da IA para documento fora do escopo."""
    return {
        "tipo_identificado": "Fora do escopo de validação",
        "observacoes": f"'{tipo_cv}' não é verificado na validação do contrato.",
    }


# --------------------------------------------------------------------------
# Matriz de documentos obrigatórios por modalidade (cartilha oficial da
# área de contratos, 31/08/2026)
# --------------------------------------------------------------------------

# Nomes internos das modalidades. O texto real que vem de "tipovenda" (via
# GET /v1/cvdw/reservas) é mapeado pra um desses três em
# classificar_modalidade() - só "Venda Loteamento" foi confirmado
# empiricamente até agora (29/08/2026); os textos de Financiamento Bancário
# e Venda Direta ainda não apareceram num caso real, então o mapeamento usa
# palavras-chave prováveis e cai em None (modalidade desconhecida) se não
# reconhecer - nesse caso as regras específicas de modalidade não são
# aplicadas (só as comuns a todas), em vez de arriscar classificar errado.
MODALIDADE_FINANCIAMENTO = "financiamento_bancario"
MODALIDADE_VENDA_DIRETA = "venda_direta"
MODALIDADE_LOTEAMENTO = "loteamento"

# Documentos exigidos em QUALQUER modalidade de pessoa física (identificação,
# certidão de estado civil, comprovante de endereço). Comparação por trecho
# (case-insensitive) contra o "tipo" cadastrado no CV, igual ao padrão já
# usado em TIPOS_FORA_DO_ESCOPO_DE_VALIDACAO - exceto as siglas curtas (rg,
# cpf, cnh, crnm), que desde 09/10/2026 só casam como palavra inteira (ver
# termo_no_texto()).
TERMOS_IDENTIFICACAO = ["rg", "cpf", "cnh", "identidade", "carteira funcional",
                        "passaporte", "crnm"]
DOCUMENTOS_COMUNS_TODAS_MODALIDADES = [
    TERMOS_IDENTIFICACAO,
    ["certidão de estado civil", "certidao de estado civil"],
    ["comprovante de endereço", "comprovante de endereco"],
]

# Termos que identificam o documento de análise de risco da CEF. Fica numa
# constante só porque ele é usado em DOIS lugares (exigência por modalidade e
# dispensa por modalidade) - quando as duas listas viviam separadas elas
# divergiram na prática.
#
# "Tela de Aprovação" entrou aqui em 09/09/2026: é o nome que o CV dá ao slot
# desse documento na reserva 33773 (primeiro financiamento CEF real testado).
# Confirmado abrindo o PDF - o cabeçalho da página 1 diz literalmente
# "Formulário de Impressão - AVALIAÇÃO DE RISCO", e a própria IA identificou o
# arquivo como "Tela de Avaliação de Risco". É o mesmo artefato da cartilha, só
# rotulado diferente no CV. Antes dessa correção o sistema acusava "documento
# obrigatório ausente" com o documento anexado na pasta (falso positivo).
TERMOS_TELA_DE_RISCO = [
    "tela de avaliação de risco",
    "tela de avaliacao de risco",
    "tela de aprovação",
    "tela de aprovacao",
    "cadastro cef",
    "avaliação de risco",
    "avaliacao de risco",
]

# Termos que identificam o comprovante de renda - no nome do campo do CV e no
# 'tipo_identificado' da IA (ver ausencias_apos_leitura()). Constante própria
# porque a varredura (varrer_arquivos_repetidos.py) a usa, e até 13/10/2026
# tinha uma cópia. Desde 14/10/2026 ela NÃO entra na exigência (ver
# DOCUMENTOS_ADICIONAIS_POR_MODALIDADE).
#
# Holerite/contracheque entram porque é assim que a IA costuma nomear o
# arquivo; sem eles, um holerite anexado em "Documentos Diversos" viraria
# "comprovante de renda ausente" em vez de "campo errado". Extrato bancário e
# afins NÃO entram: renda informal é pergunta em aberto (pergunta 1).
# "Demonstrativo de pagamento" também NÃO (revisão de 13/10/2026): em venda de
# imóvel ele pode ser o demonstrativo da parcela/boleto, e aí um papel da
# própria venda passaria por comprovante de renda.
TERMOS_COMPROVANTE_DE_RENDA = [
    "comprovante de renda",
    "comprovante de rendimento",
    "holerite",
    "contracheque",
]

# Documentos adicionais por modalidade, além dos comuns acima.
#
# Comprovante de renda NÃO é exigido (gestora comercial, e-mail de 14/10/2026): o setor de
# contratos não apura renda, só confere se a renda do CV bate com a Tela de
# Avaliação de Risco. O João tinha passado a exigi-lo do titular em 13/10/2026 e
# reverteu a decisão no dia seguinte. Anexado, ele continua sendo lido, e o que
# a leitura achar vira aviso (documento não exigido).
DOCUMENTOS_ADICIONAIS_POR_MODALIDADE: dict[str, list[list[str]]] = {
    MODALIDADE_FINANCIAMENTO: [
        TERMOS_TELA_DE_RISCO,
    ],
    MODALIDADE_VENDA_DIRETA: [
        # Sem exigência adicional além dos comuns, pra pessoa física.
    ],
    MODALIDADE_LOTEAMENTO: [
        ["pesquisa cadastral"],
    ],
}

# Documentos que a operação precisa ter UMA VEZ, não um por pessoa.
#
# 05/10/2026, ao passar a validar o 2º proponente. Sem esta lista, TODA pasta
# de 2º comprador numa venda de financiamento acusaria "documento obrigatório
# não encontrado: tela de avaliação de risco" - um falso positivo garantido em
# ~47% das reservas, que é a proporção que tem segunda pessoa. Seria repetir
# exatamente o erro de 22/09 (cobrar o que não é exigido) numa escala nova.
#
# MEDIDO, não suposto: na varredura de 30 reservas de 28/09/2026, a Tela de
# Aprovação aparece SÓ no grupo do titular, em todas as reservas que a têm. Já
# a MO Pesquisa Cadastral aparece em grupos de 2º comprador (33947, 33974,
# 33941), então ela NÃO entra aqui - é por pessoa mesmo.
#
# ⚠️ Inferido dos dados, não da cartilha. CONFIRMAR COM A MILA: a Tela de
# Avaliação de Risco é uma por operação ou uma por proponente?
EXIGIDOS_UMA_VEZ_POR_OPERACAO: list[list[str]] = [
    TERMOS_TELA_DE_RISCO,
]

# Documentos que são da PESSOA, mas que a cartilha cobra só do titular.
#
# Diferente de EXIGIDOS_UMA_VEZ_POR_OPERACAO no motivo, igual no efeito. Nasceu
# em 13/10/2026 para o comprovante de renda, que saiu da matriz em 14/10/2026
# (ver DOCUMENTOS_ADICIONAIS_POR_MODALIDADE). Fica VAZIA, e não apagada, porque
# documentos_exigidos_da_modalidade(), documento_e_exigido() e a varredura
# (varrer_arquivos_repetidos.py) já sabem tratar documento só do titular.
EXIGIDOS_SO_DO_TITULAR: list[list[str]] = []

# Tipos de documento que a cartilha confirma NÃO fazerem parte da validação
# em NENHUMA modalidade (reforça TIPOS_FORA_DO_ESCOPO_DE_VALIDACAO, que já
# cobre CTPS - mantido aqui só como referência, a lista de verdade
# continua sendo TIPOS_FORA_DO_ESCOPO_DE_VALIDACAO).


def classificar_modalidade(tipovenda: str | None) -> str | None:
    """Mapeia o texto de 'tipovenda' (vindo de GET /v1/cvdw/reservas) pra
    uma das 3 modalidades da cartilha. Retorna None se não reconhecer o
    texto - nesse caso o chamador deve aplicar só as regras comuns a todas
    as modalidades, nunca chutar uma modalidade específica."""
    if not tipovenda:
        return None
    texto = tipovenda.strip().lower()
    if "loteamento" in texto:
        return MODALIDADE_LOTEAMENTO
    if "financiamento" in texto:
        return MODALIDADE_FINANCIAMENTO
    if "direta" in texto or "à vista" in texto or "a vista" in texto:
        return MODALIDADE_VENDA_DIRETA
    return None


def numero_da_pasta_e_idreserva(nome_pasta: str) -> bool:
    """Diz se o número no fim do nome da pasta é um idRESERVA (padrão
    '...-reserva-33937', gerado por mvp_v2_reserva.py) ou um idprecadastro
    ('...-34243', gerado por mvp_v1_validador.py).

    Isso importa porque o atalho de busca direta só vale pra idreserva -
    chamar GET /v1/comercial/reservas/{n} com um idprecadastro traria a
    reserva de outra pessoa, silenciosamente."""
    return bool(re.search(r"-reserva-\d+$", nome_pasta))


def buscar_registro_da_reserva(
    cfg: dict, idprecadastro_ou_idreserva: int, eh_idreserva: bool = False
) -> dict | None:
    """Devolve o texto de 'tipovenda' da reserva. Retorna None quando o
    pré-cadastro ainda não virou reserva (ou não foi encontrado) - nesse caso
    a modalidade é desconhecida, o que não é erro.

    Dois caminhos, do mais barato pro mais caro:

    1. 'eh_idreserva=True' -> GET /v1/comercial/reservas/{id}, UMA requisição.
       Se responder com 'tipovenda', acabou.
    2. Procura na listagem /v1/cvdw/reservas varrendo DE TRÁS PRA FRENTE,
       porque reserva recente está nas últimas páginas.

    Histórico, porque errei duas vezes aqui e vale ficar registrado:

    - Até 22/09/2026 existia só a varredura, do começo ao fim, baixando as
      ~27 mil reservas (54 páginas) pra ler UM campo. Era a maior parte do
      tempo de execução - mais que a leitura por IA, que é o trabalho real.
    - A primeira tentativa de otimizar (22/09) adicionou o caminho 1 e uma
      "parada antecipada" na varredura, e NÃO resolveu: o caminho 1 falhou em
      silêncio (pela tela era impossível saber) e a parada antecipada não
      ajuda numa reserva RECENTE, que está no fim da lista, não no começo.
    - O que funciona é varrer ao contrário e dizer em voz alta qual caminho
      foi usado."""
    # Caminho 1: busca direta (só quando temos certeza de que é idreserva).
    if eh_idreserva:
        reserva = buscar_reserva_individual(cfg, idprecadastro_ou_idreserva)
        if reserva and reserva.get("tipovenda"):
            print("      modalidade obtida em 1 requisição (busca direta)")
            return reserva
        # NUNCA falhar em silêncio aqui: na primeira versão desta otimização o
        # fallback era mudo, e da tela do terminal era impossível saber se o
        # atalho tinha funcionado ou não.
        if reserva is None:
            print("      busca direta da reserva não respondeu - usando a listagem")
        else:
            print("      busca direta respondeu mas sem o campo 'tipovenda' - usando a listagem")

    # Caminho 3: procura na listagem, varrendo de trás pra frente (reserva
    # recente está nas últimas páginas).
    campo_alvo = "idreserva" if eh_idreserva else "idprecadastro"
    try:
        reserva = procurar_reserva_na_listagem(
            cfg, campo_alvo, idprecadastro_ou_idreserva
        )
    except SystemExit:
        return None

    if reserva is None and not eh_idreserva:
        # Pasta de pré-cadastro cujo idprecadastro não aparece na listagem: o
        # pré-cadastro ainda não virou reserva, e a modalidade é desconhecida.
        #
        # Até 09/10/2026 aqui havia um "tenta também como idreserva" (o
        # comportamento antigo de tentar os dois campos). É a armadilha 5 ao
        # contrário, da mesma família do caso 34239: idprecadastro e idreserva
        # são numerações DIFERENTES na mesma faixa (o pré-cadastro 34243 virou a
        # reserva 33609), então a reserva de número igual é de OUTRA pessoa - e a
        # modalidade dela passava a decidir o que esta pasta tinha que ter (Tela
        # de Risco, Pesquisa Cadastral). Nenhum fluxo grava pasta '{nome}-{N}'
        # com N = idreserva: o mvp_v1_validador.py usa o idprecadastro, e o
        # mvp_v2_reserva.py sempre usa '-reserva-{id}' (desde 29/08/2026, quando
        # nasceu, no mesmo dia do particionamento de saidas/).
        print("      pré-cadastro sem reserva na listagem: modalidade desconhecida, "
              "só regras comuns (o mesmo número como idreserva seria a reserva de "
              "outra pessoa)")

    return reserva


def buscar_tipovenda_por_idprecadastro(
    cfg: dict, idprecadastro_ou_idreserva: int, eh_idreserva: bool = False
) -> str | None:
    """Só o texto de 'tipovenda'. Mantida porque outros pontos do projeto
    chamam por este nome; internamente usa buscar_registro_da_reserva()."""
    registro = buscar_registro_da_reserva(cfg, idprecadastro_ou_idreserva, eh_idreserva)
    return registro.get("tipovenda") if registro else None


def documentos_exigidos_da_modalidade(
    modalidade: str | None, papel: str | None = None,
) -> list[list[str]]:
    """Lista de documentos exigidos (cada um como uma lista de termos
    alternativos, ex: ["rg","cpf","cnh",...] conta como UM documento
    satisfeito por qualquer um desses tipos). Modalidade desconhecida
    (None) retorna só os documentos comuns a todas - nunca aplica uma
    exigência específica sem confirmar a modalidade.

    'papel' é quem é o dono desta pasta na operação: None ou "titular" para o
    titular, o tipo_associacao ("2º Promissário Comprador", "Avalista 1"...)
    para os demais. Para quem não é o titular, os documentos de
    EXIGIDOS_UMA_VEZ_POR_OPERACAO saem da lista - eles pertencem à operação,
    não à pessoa, e cobrá-los de cada proponente geraria falso positivo
    garantido. Os de EXIGIDOS_SO_DO_TITULAR também saem (comprovante de renda,
    13/10/2026). O default mantém o comportamento antigo de quem chama sem o
    parâmetro (varrer_arquivos_repetidos.py, por exemplo)."""
    exigidos = list(DOCUMENTOS_COMUNS_TODAS_MODALIDADES)
    if modalidade:
        exigidos += DOCUMENTOS_ADICIONAIS_POR_MODALIDADE.get(modalidade, [])
    if not papel_e_titular(papel):
        exigidos = [e for e in exigidos
                    if e not in EXIGIDOS_UMA_VEZ_POR_OPERACAO
                    and e not in EXIGIDOS_SO_DO_TITULAR]
    return exigidos


def papel_e_titular(papel: str | None) -> bool:
    """Pasta sem papel declarado é do titular - é o caso de toda pasta gerada
    antes de 05/10/2026 e de todo pré-cadastro."""
    return not papel or papel.strip().lower() == "titular"


# Siglas curtas de documento de identificação: só valem como PALAVRA INTEIRA.
#
# 09/10/2026, verificação offline do projeto. Comparadas por trecho, 'rg' casava
# com "eneRGia" e "caRGo": numa pasta SEM documento de identificação, uma conta
# de energia lida pela IA fazia ausencias_apos_leitura() dizer "o documento
# existe, é o campo do CV que está errado" - e a falta, que trava a venda, virava
# aviso. O mesmo trecho fazia "Conta de Energia" ser tratada como campo de
# identificação em documento_e_exigido() e familia_do_campo().
#
# Os demais termos continuam por trecho, de propósito: são longos ("carteira de
# identidade", "comprovante de endereço") e o trecho é o que tolera variação de
# redação do CV e da IA.
SIGLAS_DE_IDENTIFICACAO = {"rg", "cpf", "cnh", "crnm", "rne"}


def termo_no_texto(termo: str, texto: str) -> bool:
    """'termo' aparece em 'texto' (ambos já em minúsculas)? Sigla curta só como
    palavra inteira - "(RG)", "RG/CPF" e "RG, CPF, CNH ou OUTROS" casam;
    "energia", "cargo" e "órgão" não."""
    if termo in SIGLAS_DE_IDENTIFICACAO:
        return re.search(rf"(?<![^\W_]){termo}(?![^\W_])", texto) is not None
    return termo in texto


def documento_satisfaz(termos: list[str], tipos_presentes: list[str]) -> bool:
    tipos_lower = [t.lower() for t in tipos_presentes]
    return any(termo_no_texto(termo, tipo) for termo in termos for tipo in tipos_lower)


def checar_documentos_ausentes(
    modalidade: str | None, tipos_presentes: list[str], papel: str | None = None,
) -> list[str]:
    """Compara a lista de documentos exigidos pela modalidade com os tipos
    de documento que realmente vieram na pasta (coluna 'tipo' da aba
    Documentos). Retorna uma divergência por documento obrigatório
    ausente. Não gasta chamada de IA - é comparação de texto, feita ANTES
    de mandar qualquer documento pra leitura."""
    divergencias = []
    for termos in documentos_exigidos_da_modalidade(modalidade, papel):
        if not documento_satisfaz(termos, tipos_presentes):
            nome_amigavel = termos[0]
            divergencias.append(f"Documento obrigatório não encontrado na pasta: '{nome_amigavel}'")
    return divergencias


# --------------------------------------------------------------------------
# O ARQUIVO é mesmo do tipo do CAMPO onde foi anexado?
#
# 06/10/2026, reserva 34428 (pré-triagem). A "Certidão de Estado Civil" da
# titular era uma CERTIDÃO DE BATISMO - documento de igreja, não de registro
# civil. A IA identificou certinho e escreveu nas observações que não batia com
# o tipo cadastrado. O código não comparava esse campo com nada: conferia só o
# RÓTULO do campo no CV, nunca o que o arquivo é de fato.
#
# Ou seja: a ferramenta já sabia e ficou calada. Custo de corrigir: zero
# chamada de IA nova - 'tipo_identificado' já vem em toda leitura.
#
# DESENHO CONSERVADOR, de propósito. O 'tipo_identificado' é texto livre da IA
# e varia muito; sair apontando toda vez que ele não casasse com uma lista
# minha recriaria o problema dos 100% de ruído. Então só fala quando tem
# fundamento:
#
#   1. o tipo identificado está na lista de QUE NÃO SERVEM pra esse campo
#      (batismo como estado civil), ou
#   2. o tipo identificado é claramente de OUTRA família conhecida
#      (uma conta de luz no campo de certidão).
#
# Vocabulário que eu não reconheço NÃO vira apontamento - fica em silêncio.
# Melhor perder um caso do que inundar o relatório.
# --------------------------------------------------------------------------

FAMILIAS_DE_DOCUMENTO: dict[str, dict] = {
    "identificação": {
        "campos": ["rg", "cpf", "cnh", "identidade", "carteira funcional",
                   "passaporte", "crnm"],
        "aceitos": ["rg", "registro geral", "carteira de identidade", "identidade",
                    "cnh", "carteira nacional de habilitacao", "habilitacao",
                    "cpf", "cadastro de pessoa", "passaporte", "crnm", "rne",
                    "carteira funcional", "documento de identificacao", "identificacao"],
        # Identificação precisa ser documento COM FOTO (decisão do João,
        # 13/10/2026). O "Comprovante de Situação Cadastral no CPF" da Receita
        # tem "CPF" no nome e casaria com 'cpf' acima, mas não identifica
        # ninguém. O cartão do CPF continua aceito. Os termos estão sem acento
        # e em minúsculas porque são comparados com _sem_acento(). Ver também
        # so_comprovante_de_cpf_no_campo() em ausencias_apos_leitura(), e
        # IDENTIFICACAO_COM_FOTO (arquivo com RG + comprovante juntos serve).
        "nao_servem": ["situacao cadastral", "comprovante de inscricao",
                       "inscricao no cpf"],
        # O campo do CV se chama "RG, CPF, CNH ou OUTROS" - esse "ou outros" é
        # um convite a aceitar vários documentos, então aqui a divergência
        # seria presunçosa. Vira aviso.
        "severidade": "aviso",
    },
    "certidão de estado civil": {
        "campos": ["certidao de estado civil"],
        "aceitos": ["certidao de nascimento", "nascimento", "certidao de casamento",
                    "casamento", "certidao de obito", "obito", "averbacao",
                    "divorcio", "uniao estavel", "estado civil"],
        "nao_servem": ["batismo", "crisma", "primeira comunhao"],
        "severidade": "divergencia",
    },
    "comprovante de endereço": {
        "campos": ["comprovante de endereco"],
        "aceitos": ["comprovante de endereco", "endereco", "residencia",
                    "conta de luz", "conta de agua", "conta de gas", "energia",
                    "fatura", "boleto", "telefone", "internet", "nota fiscal",
                    "contrato de aluguel", "contrato de locacao", "iptu"],
        "severidade": "divergencia",
    },
    "comprovante de renda": {
        "campos": ["comprovante de renda"],
        "aceitos": ["holerite", "contracheque", "demonstrativo de pagamento",
                    "folha de pagamento", "extrato bancario", "extrato de conta",
                    "extrato", "declaracao de renda", "imposto de renda", "irpf",
                    "decore", "pro-labore", "prolabore", "beneficio", "inss",
                    "carteira de trabalho", "ctps", "renda"],
        # Comprovação de renda aceita muita coisa diferente, formal e informal.
        # Ver a pergunta 1 em perguntas-para-mila.md.
        "severidade": "aviso",
    },
}


# Tipos que não satisfazem exigência NENHUMA da cartilha, em campo nenhum.
#
# Acrescentado depois de rodar a checagem contra os 144 documentos já
# processados (06/10/2026): ela acertou o batismo e ficou MUDA numa reserva de
# venda PJ, onde a IA identificou o arquivo como "Contrato de Confissão de Dívida
# e Dação em Pagamento" e ele estava em RG, certidão, comprovante de endereço e
# comprovante de renda ao mesmo tempo. Caía na regra 4 (vocabulário
# desconhecido -> silêncio), que é prudente demais pra um caso tão claro.
#
# Ali a detecção de arquivo repetido já pegava pelo CONJUNTO. Mas se alguém
# anexar um contrato de dação em UM campo só, nada pegaria - e é isso que esta
# lista fecha.
#
# Cuidado ao crescer esta lista: "contrato" sozinho NÃO entra, porque contrato
# de aluguel/locação é comprovante de endereço legítimo.
TIPOS_QUE_NAO_SATISFAZEM_EXIGENCIA = [
    "dacao em pagamento",
    "confissao de divida",
    "contrato de compra e venda",
    "distrato",
    "procuracao",
]


def _sem_acento(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def familia_do_campo(tipo_cv: str) -> str | None:
    """A qual família de documento pertence o CAMPO do CV onde isto foi
    anexado. None = campo que esta checagem não cobre (CTPS, Simulação,
    Tela de Aprovação, Documentos Diversos...), e aí ela não opina."""
    alvo = _sem_acento(tipo_cv)
    for nome, fam in FAMILIAS_DE_DOCUMENTO.items():
        if any(termo_no_texto(c, alvo) for c in fam["campos"]):
            return nome
    return None


def checar_tipo_do_documento(tipo_cv: str, tipo_identificado: str) -> dict:
    """O que a IA identificou bate com o campo onde o documento foi anexado?

    Devolve {"divergencias": [...], "avisos": [...]}. Silêncio quando não há
    fundamento pra falar - ver o bloco de comentário acima.
    """
    vazio: dict[str, list[str]] = {"divergencias": [], "avisos": []}
    familia = familia_do_campo(tipo_cv)
    if not familia or not str(tipo_identificado or "").strip():
        return vazio

    fam = FAMILIAS_DE_DOCUMENTO[familia]
    identificado = _sem_acento(tipo_identificado)

    def emitir(msg: str) -> dict:
        chave = "divergencias" if fam.get("severidade") == "divergencia" else "avisos"
        return {"divergencias": [], "avisos": [], chave: [msg]}

    # 1. Tipo que sabidamente NÃO serve pra este campo - nem o que não serve
    #    pra campo nenhum.
    for termo in list(fam.get("nao_servem", [])) + TIPOS_QUE_NAO_SATISFAZEM_EXIGENCIA:
        # Identificação: o comprovante de situação cadastral junto com um
        # documento com foto no mesmo arquivo não é troca de documento - ver
        # IDENTIFICACAO_COM_FOTO. As outras famílias não mudam.
        if (familia == "identificação" and termo in fam["nao_servem"]
                and not nao_serve_como_identificacao(tipo_identificado)):
            continue
        if termo in identificado:
            return emitir(
                f"O campo '{tipo_cv}' está preenchido com um documento identificado como "
                f"'{tipo_identificado}', que não serve como {familia}"
            )

    # 2. Bate com o esperado - nada a dizer.
    if any(termo_no_texto(termo, identificado) for termo in fam["aceitos"]):
        return vazio

    # 3. Não bate com o esperado, mas bate com OUTRA família conhecida: é troca
    #    de documento, não vocabulário novo.
    for outro_nome, outra in FAMILIAS_DE_DOCUMENTO.items():
        if outro_nome == familia:
            continue
        if any(termo_no_texto(termo, identificado) for termo in outra["aceitos"]):
            return emitir(
                f"O campo '{tipo_cv}' está preenchido com um documento identificado como "
                f"'{tipo_identificado}', que parece ser {outro_nome} e não {familia}"
            )

    # 4. Vocabulário que não reconheço. Cala a boca em vez de chutar.
    return vazio


# Documentos com foto que, aparecendo no MESMO 'tipo_identificado' do
# comprovante de situação cadastral, fazem o arquivo valer como identificação.
#
# 13/10/2026, medição em saidas/ (59 pastas): as 2 únicas mudanças do ajuste da
# identificação eram falso positivo. Um PDF só trazia o RG e o comprovante, e a
# IA leu "RG e Comprovante de Situação Cadastral no CPF" - o arquivo virava
# "identificação ausente". "cpf" solto NÃO está aqui: ele faz parte do nome do
# próprio comprovante; só o cartão conta. Siglas casam como palavra inteira
# (termo_no_texto), então "energia" e "cargo" não contam como RG.
IDENTIFICACAO_COM_FOTO = ["rg", "cnh", "identidade", "carteira de identidade",
                          "habilitacao", "passaporte", "crnm", "rne",
                          "carteira funcional", "cartao do cpf", "cartao cpf"]


def nao_serve_como_identificacao(tipo_identificado: str) -> bool:
    """A IA leu o arquivo como um documento que NÃO vale como identificação
    (o comprovante de situação cadastral no CPF - ver 'nao_servem' em
    FAMILIAS_DE_DOCUMENTO["identificação"])? Arquivo que traz junto um
    documento com foto (IDENTIFICACAO_COM_FOTO) serve."""
    identificado = _sem_acento(tipo_identificado)
    if not any(termo in identificado
               for termo in FAMILIAS_DE_DOCUMENTO["identificação"]["nao_servem"]):
        return False
    return not any(termo_no_texto(termo, identificado) for termo in IDENTIFICACAO_COM_FOTO)


def so_comprovante_de_cpf_no_campo(
    termos: list[str], tipos_presentes: list[str], resultados: list[dict],
) -> bool:
    """O campo de identificação está preenchido, mas TODOS os arquivos dele são
    comprovante de situação cadastral no CPF?

    13/10/2026, decisão do João: identificação precisa ser documento com foto.
    checar_tipo_do_documento() já avisa arquivo por arquivo; mas se não sobra
    NENHUM documento de identificação de verdade, o que existe é falta - que
    trava a venda - e não troca de documento. Por isso só vale quando todos os
    arquivos do campo foram lidos e todos são desse tipo: arquivo sem leitura
    (ou com leitura-sentinela) não é prova de nada, e aí o campo continua
    contando como preenchido.

    "Todos os arquivos" é contado em 'tipos_presentes' (um item por documento
    da aba Documentos), não em 'resultados': documento sem arquivo baixado,
    arquivo que sumiu da pasta e linha que o cache não trouxe NÃO geram
    registro em 'resultados'. Se os dois números não batem, há documento de
    identificação que ninguém leu - e na dúvida não se acusa ausência.
    """
    if termos != TERMOS_IDENTIFICACAO:
        return False
    do_campo = [r for r in resultados
                if documento_satisfaz(termos, [str(r.get("tipo_cv") or "")])]
    no_cv = [t for t in tipos_presentes if documento_satisfaz(termos, [t or ""])]
    if not do_campo or len(do_campo) != len(no_cv):
        return False
    return all(
        nao_serve_como_identificacao(str((r.get("doc_ia") or {}).get("tipo_identificado") or ""))
        for r in do_campo
    )


# Certidão de estado civil: o que vale e o que não vale (gestora comercial, 14/10/2026).
#
# Valem nascimento, casamento, averbação (de divórcio, de separação) e óbito.
# Certidão religiosa NÃO vale: é documento de igreja, não de registro civil
# (caso real: batismo no campo de estado civil, reserva 34428, 06/10/2026). A
# união estável não muda o estado civil: serve de apoio na conferência, mas
# sozinha não substitui a certidão. Termos sem acento e em minúsculas, para
# comparar com _sem_acento().
TERMOS_CERTIDAO_VALIDA = ["nascimento", "casamento", "averbacao", "divorcio",
                          "separacao", "obito"]
TERMOS_CERTIDAO_RELIGIOSA = ["batismo", "crisma", "primeira comunhao"]
TERMOS_UNIAO_ESTAVEL = ["uniao estavel"]


def motivo_certidao_nao_vale(tipo_identificado: str) -> str | None:
    """Se a leitura mostra um documento que NÃO vale como certidão de estado
    civil, devolve o motivo; senão None. Leitura com tipo válido, genérico
    ("Certidão de Estado Civil") ou desconhecido devolve None - na dúvida não se
    acusa ausência (mesmo desenho conservador de checar_tipo_do_documento())."""
    t = _sem_acento(tipo_identificado)
    if not t or any(termo in t for termo in TERMOS_CERTIDAO_VALIDA):
        return None
    if any(termo in t for termo in TERMOS_CERTIDAO_RELIGIOSA):
        return (f"o arquivo é '{tipo_identificado}', certidão religiosa, que não "
                f"comprova estado civil")
    if any(termo in t for termo in TERMOS_UNIAO_ESTAVEL):
        return (f"o arquivo é '{tipo_identificado}': a união estável serve de apoio, "
                f"mas não substitui a certidão de nascimento, casamento, averbação ou óbito")
    return None


def certidao_do_campo_nao_vale(
    termos: list[str], tipos_presentes: list[str], resultados: list[dict],
) -> str | None:
    """O campo de certidão de estado civil está preenchido, mas TODOS os
    arquivos dele são documentos que não valem (motivo_certidao_nao_vale())?
    Devolve o motivo, ou None.

    Mesma cautela de so_comprovante_de_cpf_no_campo(): só conclui quando todos
    os arquivos do campo foram lidos. Arquivo sem leitura não prova nada."""
    if "certidao de estado civil" not in termos:
        return None
    do_campo = [r for r in resultados
                if documento_satisfaz(termos, [str(r.get("tipo_cv") or "")])]
    no_cv = [t for t in tipos_presentes if documento_satisfaz(termos, [t or ""])]
    if not do_campo or len(do_campo) != len(no_cv):
        return None
    motivos = [motivo_certidao_nao_vale(str((r.get("doc_ia") or {}).get("tipo_identificado") or ""))
               for r in do_campo]
    if not all(motivos):
        return None
    return "; ".join(sorted(set(motivos)))


def ausencias_apos_leitura(
    modalidade: str | None, papel: str | None,
    tipos_presentes: list[str], resultados: list[dict],
    certidao_do_conjuge: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Reavalia os documentos obrigatórios DEPOIS da leitura por IA.

    06/10/2026, amostra de 10 reservas em "Contrato Aguardando Validação".
    Quatro delas acusaram 'tela de avaliação de risco' ausente - 40% dos
    achados da amostra. Três eram reais. A da JANAINA (34237) não: ela tem um
    arquivo no campo "Formulários CEF Emitido" que a IA identificou como
    **Cadastro CEF** - e 'cadastro cef' está em TERMOS_TELA_DE_RISCO. O
    documento exigido estava lá, num campo de outro nome.

    A causa é estrutural: checar_documentos_ausentes() compara os termos
    exigidos com o NOME DO CAMPO no CV, e nunca com o que o arquivo é de fato.
    Era a única informação disponível antes da IA ler - mas depois que ela lê,
    'tipo_identificado' responde justamente isso, e ninguém olhava.

    Importa porque é a diferença entre "falta documento obrigatório" e "o
    documento está no campo errado". A primeira trava a venda; a segunda é
    arrumação de cadastro. Apresentar uma como a outra é o jeito mais rápido de
    a ferramenta perder credibilidade - basta alguém abrir a reserva e ver o
    documento lá.

    14/10/2026 (gestora comercial): certidão religiosa e união estável sozinha no campo de
    estado civil contam como certidão AUSENTE (certidao_do_campo_nao_vale()).
    E a certidão que falta pode ser a de casamento do cônjuge, anexada na pasta
    dele: 'certidao_do_conjuge' é o papel do cônjuge (ver certidao_do_conjuge()),
    e aí a falta vira aviso.

    Devolve (faltam_de_verdade, anexados_em_campo_errado, avisos_do_conjuge).
    """
    faltam: list[str] = []
    campo_errado: list[str] = []
    avisos_conjuge: list[str] = []

    for termos in documentos_exigidos_da_modalidade(modalidade, papel):
        so_cpf = so_comprovante_de_cpf_no_campo(termos, tipos_presentes, resultados)
        certidao_nao_vale = certidao_do_campo_nao_vale(termos, tipos_presentes, resultados)
        if documento_satisfaz(termos, tipos_presentes) and not so_cpf and not certidao_nao_vale:
            continue

        if "certidao de estado civil" in termos and certidao_do_conjuge:
            avisos_conjuge.append(
                f"Certidão de estado civil desta pessoa "
                f"{'não vale (' + certidao_nao_vale + ')' if certidao_nao_vale else 'não anexada'}, "
                f"mas a certidão de casamento lida na pasta do {certidao_do_conjuge} vale para "
                f"os dois: casados entre si no CV (gestora comercial, 14/10/2026)"
            )
            continue

        onde = []
        for r in resultados:
            tipo_ia = str((r.get("doc_ia") or {}).get("tipo_identificado") or "")
            if tipo_ia in LEITURAS_SEM_IA:
                continue
            # O comprovante de situação cadastral tem "CPF" no nome e casaria
            # com 'cpf' - não pode contar como "a identificação existe noutro
            # campo" (13/10/2026).
            if termos == TERMOS_IDENTIFICACAO and nao_serve_como_identificacao(tipo_ia):
                continue
            if "certidao de estado civil" in termos and motivo_certidao_nao_vale(tipo_ia):
                continue
            if tipo_ia and documento_satisfaz(termos, [tipo_ia]):
                onde.append(r)

        if onde:
            campos = ", ".join(sorted({str(r.get("tipo_cv") or "?") for r in onde}))
            campo_errado.append(
                f"'{termos[0]}' não está no campo próprio, mas a leitura reconheceu "
                f"este documento anexado em: {campos}. O documento existe - é o campo "
                f"do CV que está errado"
            )
        elif so_cpf:
            faltam.append(
                f"Documento obrigatório não encontrado na pasta: '{termos[0]}' - o campo "
                f"de identificação tem só comprovante de situação cadastral no CPF, que "
                f"não é documento com foto"
            )
        elif certidao_nao_vale:
            faltam.append(
                f"Documento obrigatório não encontrado na pasta: '{termos[0]}' - "
                f"{certidao_nao_vale}"
            )
        elif termos == TERMOS_TELA_DE_RISCO and any(
                fora_do_escopo_de_validacao(str(r.get("tipo_cv") or ""))
                and "cca" in str(r.get("tipo_cv") or "").lower()
                and str((r.get("doc_ia") or {}).get("tipo_identificado") or "") in LEITURAS_SEM_IA
                for r in resultados):
            # A Simulação CCA saiu do escopo (14/10/2026) e não é lida; em 34323
            # era nela que a Tela estava. Não dá para afirmar - só apontar onde olhar.
            faltam.append(
                f"Documento obrigatório não encontrado na pasta: '{termos[0]}' - há arquivo "
                f"no campo 'Simulação CCA', que não é lido (fora do escopo): confira se a "
                f"Tela não foi anexada nele"
            )
        else:
            faltam.append(
                f"Documento obrigatório não encontrado na pasta: '{termos[0]}'"
            )

    return faltam, campo_errado, avisos_conjuge


def documento_e_exigido(
    tipo_cv: str, modalidade: str | None, papel: str | None = None,
) -> bool:
    """O tipo de documento consta na lista de EXIGIDOS para esta modalidade?

    Esta é a distinção que faltava e que fazia o sistema apontar problema em
    100% das reservas já validadas (descoberto em 22/09/2026, olhando casos
    reais no status "Venda Validada"):

        a cartilha define o que é EXIGIDO.
        a ferramenta julgava tudo que estava ANEXADO.

    São conjuntos diferentes. Um correspondente anexa um PDF em branco num
    campo que ninguém exige (Extrato de FGTS numa Venda Direta, por exemplo) e
    o sistema tratava isso como defeito do contrato. Não é: é ruído.

    Documento não exigido continua sendo analisado e relatado - inclusive
    porque PDF em branco é justamente o padrão de burla que a gestora comercial descreveu -
    mas os achados dele viram AVISO, nunca divergência.

    'papel' (13/10/2026): fora do titular, os EXIGIDOS_SO_DO_TITULAR não contam
    - o comprovante de renda do 2º proponente não é exigido, então um nome
    divergente nele continua aviso, como era antes de o comprovante passar a
    ser exigido do titular. Só esses saem: EXIGIDOS_UMA_VEZ_POR_OPERACAO (Tela
    de Risco) segue como antes, de propósito. O default (None) é o titular.
    """
    tipo_lower = (tipo_cv or "").lower()
    exigidos = documentos_exigidos_da_modalidade(modalidade)
    if not papel_e_titular(papel):
        exigidos = [e for e in exigidos if e not in EXIGIDOS_SO_DO_TITULAR]
    for termos in exigidos:
        if any(termo_no_texto(termo, tipo_lower) for termo in termos):
            return True
    return False


def dispensado_pela_modalidade(tipo_cv: str, modalidade: str | None) -> str | None:
    """Se este tipo de documento é dispensado NESSA modalidade específica
    (ex: Tela de Avaliação de Risco em Loteamento/Venda Direta), devolve o
    motivo (texto curto) pra registrar como 'Fora do escopo' - não chama a
    IA nem pode virar divergência. Devolve None se o documento deve ser
    analisado normalmente."""
    if modalidade is None:
        # Modalidade desconhecida - não dispensa nada específico, só as
        # regras comuns (CTPS etc.) já cobertas por
        # TIPOS_FORA_DO_ESCOPO_DE_VALIDACAO continuam valendo.
        return None

    tipo_lower = (tipo_cv or "").lower()
    eh_documento_de_risco = any(t in tipo_lower for t in TERMOS_TELA_DE_RISCO)

    if eh_documento_de_risco and modalidade in (MODALIDADE_VENDA_DIRETA, MODALIDADE_LOTEAMENTO):
        return (
            f"Tela de Avaliação de Risco/Cadastro CEF não é exigido em "
            f"{'Venda Direta' if modalidade == MODALIDADE_VENDA_DIRETA else 'Venda Loteamento'} "
            f"(só Financiamento Bancário) - conforme a cartilha da área de contratos"
        )
    return None


PROMPT_EXTRACAO = """Você é um auditor de documentos de uma incorporadora imobiliária. Sua \
leitura alimenta uma validação de contrato que afeta pessoas de verdade - um erro seu pode \
travar uma venda ou, pior, deixar passar um documento errado/fraudado. Leia com o máximo de \
cuidado e siga as regras abaixo à risca.

Documento em análise:
- Nome do arquivo: {nome_arquivo}
- Tipo cadastrado no CV para este documento: {tipo_cv}

REGRAS OBRIGATÓRIAS (siga todas, sem exceção):

1. NUNCA invente ou complete informação que não está literalmente visível no documento. Se \
uma parte do texto está cortada, borrada, tampada ou você não tem certeza absoluta do que \
está escrito, o valor daquele campo é null - não tente adivinhar, não use o "tipo cadastrado \
no CV" pra preencher um campo que você não conseguiu ler na imagem.
2. Transcreva números (CPF, CNPJ, datas, valores em dinheiro) EXATAMENTE como estão escritos, \
dígito por dígito. Não corrija, não arredonde, não normalize um número que pareça "quase certo".
3. Para CADA campo abaixo, informe também o quão confiante você está na leitura, em \
"confianca_campos": "alta" (você leu o texto com nitidez total, sem ambiguidade), "media" \
(deu pra ler, mas há borrão, ângulo ruim, brilho, ou o caractere podia ser confundido com \
outro - ex: 0/O, 1/I, 5/S, 8/B) ou "baixa" (você está essencialmente chutando com base em \
fragmentos). Na dúvida entre dois níveis, escolha sempre o mais baixo (mais cauteloso).
4. Datas: procure primeiro por um campo explicitamente rotulado como "data de emissão", \
"expedição" ou equivalente - isso vai em "data_emissao". Se o documento TAMBÉM tiver uma data \
de validade/vencimento explícita (comum em certidões e em propostas de financiamento - \
formato "DD/MM/AAAA a DD/MM/AAAA"), registre-a separadamente em "data_validade" - não confunda \
as duas. Se houver várias datas e não for claro qual é a de emissão, deixe "data_emissao" como \
null e explique em "observacoes" quais datas você viu e por que não soube identificar - não \
escolha uma data "no chute".

IMPORTANTE SOBRE COMPROVANTE DE ENDEREÇO: quase sempre é uma CONTA DE CONSUMO (água, luz, gás, \
telefone, internet), e esse tipo de documento NÃO TEM data de emissão - tem mês de referência, \
vencimento e datas de leitura. Não force uma data de emissão que não existe: deixe \
"data_emissao" como null e preencha "data_referencia" com o mês de referência (dia 01 quando só \
houver mês/ano) ou, na falta dele, com o vencimento da fatura. Preencha também \
"mes_referencia" (formato MM/AAAA) SOMENTE com o mês de referência impresso na conta (ex: \
"Referência: 07/2026", "Mês/Ano: JUL/2026") - nunca com o mês do vencimento nem da leitura; se a \
conta não mostrar mês de referência, "mes_referencia" é null. É o mês de referência que diz se o \
comprovante está dentro do prazo, então "data_referencia" precisa vir preenchida sempre que o \
documento tiver qualquer data que o situe no tempo.

IMPORTANTE SOBRE "data_validade" (confirmado como regra que BLOQUEIA a validação em \
22/09/2026): documento de identificação vencido impede a validação do contrato. Por isso essa \
data precisa de cuidado redobrado - um erro seu aqui trava uma venda legítima ou deixa passar \
um documento vencido. Em CNH, use o campo principal "VALIDADE" (campo 4b do documento), NÃO a \
data da primeira habilitação (campo 4a) nem as datas de validade por categoria (ACC, A, B, C, \
D, E), que costumam ser diferentes e frequentemente posteriores. Se houver mais de uma data \
candidata a validade e você não tiver certeza de qual é a do documento como um todo, deixe \
"data_validade" como null e liste TODAS as datas que viu em "observacoes" - é melhor uma \
pessoa conferir do que o sistema bloquear ou liberar com base num palpite.
5. CPF/CNPJ: transcreva só os dígitos (sem pontos/traços). Se o documento mostrar um CPF/CNPJ \
parcialmente mascarado (ex: "***.456.789-**"), transcreva os dígitos visíveis e marque \
confiança "baixa" pra esse campo - não tente completar os dígitos faltantes.
6. Nome: transcreva o nome completo exatamente como impresso no documento, incluindo a grafia \
exata de acentos e a ordem dos nomes. Não abrevie nem reordene.
7. "legivel" deve ser false se qualquer parte do documento relevante para a extração acima \
(nome, CPF, data, endereço, renda) está ilegível, cortada, ou se o arquivo está em \
branco/corrompido. Documentos completamente em branco (página vazia, PDF sem conteúdo \
visível) são muito comuns neste sistema - marque "legivel": false e "tipo_identificado": \
"Documento em branco/vazio" quando isso acontecer, em vez de tentar adivinhar o tipo.
8. Se o documento tiver MAIS de uma pessoa (ex: certidão de casamento, contrato com titular e \
cônjuge, avalista junto), extraia os dados da pessoa cujo nome mais se aproxima do papel \
esperado (titular do cadastro) quando isso for identificável, e mencione em "observacoes" que \
há mais de uma pessoa no documento - não misture dados de pessoas diferentes num único campo.
9. Se o tipo de documento que você identifica na imagem for claramente diferente do "tipo \
cadastrado no CV" informado acima (ex: cadastrado como "RG" mas a imagem é nitidamente uma \
conta de luz), registre isso em "observacoes" e preencha "tipo_identificado" com o que você \
realmente vê - não force a leitura pra combinar com o tipo esperado.
10. Se o documento for uma "Tela de Avaliação de Risco", "Cadastro CEF" ou formulário \
equivalente de análise de crédito da Caixa (geralmente tem o cabeçalho "Cliente"/"CPF Cliente" \
no topo): procure especificamente a TABELA rotulada "RENDA", que tem colunas como PROPONENTE \
(CPF), OCUPAÇÃO, RENDA BRUTA, RENDA LÍQUIDA, REFERÊNCIA (mês/ano) e COMPROVANTE DE RENDA. \
Transcreva TODAS as linhas dessa tabela em "tabela_renda", uma por proponente, cada uma com o CPF \
(só dígitos) e os valores das colunas "RENDA BRUTA" e "RENDA LÍQUIDA" exatamente como impressos \
(coluna em branco ou com "-": null) - a renda é conferida por proponente, então nenhuma linha \
pode faltar. Em "renda_encontrada" use o valor da coluna "RENDA BRUTA" da PRIMEIRA linha. \
Também procure o campo \
"Validade" (formato "DD/MM/AAAA a DD/MM/AAAA") - é o período de validade da PROPOSTA de \
financiamento, registre-o em "data_validade". Se houver um campo "Resultado da Avaliação" \
(ex: "Avaliação de Risco Aprovada não Propagada"), transcreva-o em "observacoes".

ATENÇÃO CRÍTICA SOBRE "renda_encontrada" e "tabela_renda": esses campos existem EXCLUSIVAMENTE \
para a tabela RENDA descrita na regra 10 acima. Em QUALQUER outro tipo de documento eles DEVEM ser \
null, sem exceção. \
Isso vale mesmo quando o documento mostra um valor de renda claríssimo e bem rotulado: \
holerite/contracheque, Declaração de Ajuste Anual ou recibo de Imposto de Renda, extrato de \
FGTS, simulação de crédito, extrato bancário, carteira de trabalho, declaração de empregador - \
em TODOS esses, "renda_encontrada" e "tabela_renda" são null. Se quiser registrar o valor que viu, escreva-o em \
"observacoes", nunca em "renda_encontrada". O motivo é que esse campo alimenta uma comparação \
automática com o cadastro do cliente, e a única fonte de renda aceita pela empresa é a análise \
de risco da Caixa - um valor vindo de outro documento gera correção errada num cadastro real. \
Na dúvida sobre o tipo do documento, use null.
11. Responda SOMENTE com um JSON válido, sem nenhum texto, comentário ou markdown antes ou \
depois, exatamente neste formato (todos os campos são obrigatórios; use null quando não \
souber, nunca string vazia):

{{
  "tipo_identificado": "o que você acha que é esse documento (ex: RG, CPF, CNH, comprovante de endereço, certidão de casamento, holerite, extrato bancário, contrato social, cartão CNPJ, tela de avaliação de risco, cadastro CEF, documento em branco/vazio, outro)",
  "nome_encontrado": "nome completo da pessoa, se aparecer, senão null",
  "cpf_encontrado": "somente os números do CPF, se aparecer, senão null. Em CNH, é SÓ o campo 'CPF' - nunca o Nº REGISTRO, o número do espelho ou o DOC. IDENTIDADE (RG)",
  "registro_cnh": "SÓ em CNH: somente os números do Nº REGISTRO da habilitação (11 dígitos). null em qualquer outro documento",
  "cnpj_encontrado": "somente os números do CNPJ, se aparecer, senão null",
  "data_emissao": "data de emissão/expedição no formato AAAA-MM-DD, se identificada com segurança, senão null",
  "data_referencia": "a data que melhor diz DE QUANDO este documento é, no formato AAAA-MM-DD. Em conta de consumo (água, luz, gás, telefone, internet), que não tem data de emissão, use o MÊS DE REFERÊNCIA (use o dia 01 quando só houver mês/ano) ou, na falta dele, o VENCIMENTO da fatura. Em documento que já tem data de emissão clara, repita a mesma data aqui. null só se realmente não houver nenhuma data que situe o documento no tempo",
  "mes_referencia": "SÓ em conta de consumo/comprovante de endereço: o mês de referência impresso na conta, no formato MM/AAAA (nunca o mês do vencimento ou da leitura). null se não houver mês de referência ou se não for comprovante de endereço",
  "data_validade": "data de validade/vencimento explícita (documento ou proposta), no formato AAAA-MM-DD, se houver, senão null",
  "endereco_encontrado": "endereço completo como está escrito no documento, se aparecer, senão null",
  "renda_encontrada": "valor de RENDA BRUTA em reais da PRIMEIRA linha da tabela RENDA, SOMENTE se este documento for Tela de Avaliação de Risco / Cadastro CEF (regra 10). Em holerite, imposto de renda, FGTS, simulação, extrato ou qualquer outro documento: null, mesmo que haja renda visível",
  "tabela_renda": "SOMENTE em Tela de Avaliação de Risco / Cadastro CEF (regra 10): lista com TODAS as linhas da tabela RENDA, no formato [{{\\"cpf\\": \\"somente os números\\", \\"renda_bruta\\": \\"valor como impresso ou null\\", \\"renda_liquida\\": \\"valor como impresso ou null\\"}}, ...]. Em qualquer outro documento: null",
  "legivel": true ou false,
  "confianca_campos": {{
    "nome_encontrado": "alta", "media", "baixa" ou null (se o campo já é null),
    "cpf_encontrado": "alta", "media", "baixa" ou null,
    "cnpj_encontrado": "alta", "media", "baixa" ou null,
    "data_emissao": "alta", "media", "baixa" ou null,
    "data_validade": "alta", "media", "baixa" ou null,
    "endereco_encontrado": "alta", "media", "baixa" ou null,
    "renda_encontrada": "alta", "media", "baixa" ou null,
    "tabela_renda": "alta", "media", "baixa" ou null
  }},
  "observacoes": "qualquer coisa relevante: ambiguidades, mais de uma pessoa no documento, tipo diferente do esperado, motivo de baixa confiança, resultado da avaliação de risco, etc. - resposta curta e objetiva"
}}
"""


# --------------------------------------------------------------------------
# Cache PERSISTENTE da leitura da IA (decisão do João, 19/10/2026: "se já foi
# lido uma vez, não precisa ler de novo").
#
# Medido em 19/10/2026: o cache antigo (a aba "Divergencias (IA)" da planilha da
# pessoa) nunca valia no fluxo de reserva - processar_reserva_completa() regrava
# a planilha com gerar_excel() antes da análise, a aba some, e de 13 a 19/10
# foram 0 documentos do cache e 252 a 491 por dia para a IA.
#
# Agora a chave é o sha256 do CONTEÚDO do arquivo, guardada fora da planilha
# (saidas/cache_ia/<2 primeiros>/<sha256>.json): o mesmo arquivo nunca é relido,
# mesmo que a planilha seja recriada, o arquivo baixado de novo, renomeado ou de
# outra pasta (o comprovante do casal é o mesmo papel). A versão do prompt fica
# gravada só como informação: releitura é sempre explícita (--forcar, --reler-telas).
#
# 2 P1 da Forja (19/10/2026):
# - Só entra leitura VÁLIDA e completa (leitura_valida): JSON da IA com os campos
#   mínimos do prompt e o tipo identificado. Falha, resposta cortada ou leitura
#   antiga incompleta (havia 20 assim nas planilhas) não entram.
# - A carga pelas planilhas só importa quando há PROVA de que a leitura é do
#   arquivo que está na pasta (prova_da_planilha). Sem prova, a IA lê de novo.
# --------------------------------------------------------------------------
PASTA_CACHE_IA = PASTA_SAIDAS / "cache_ia"
VERSAO_PROMPT = hashlib.sha256(PROMPT_EXTRACAO.encode("utf-8")).hexdigest()[:12]

# Os campos que o prompt pede e que toda leitura de verdade traz (valor null vale).
CAMPOS_MINIMOS_DA_LEITURA = ("tipo_identificado", "nome_encontrado", "cpf_encontrado",
                             "data_emissao", "data_validade", "legivel", "confianca_campos")

# Prova da carga pelas planilhas. O download regrava o arquivo (mtime = hora do
# download) e, no fluxo de reserva, gerar_excel() recria a planilha SEM a aba de
# leitura logo depois: toda leitura que está na aba foi feita pela IA na mesma
# rodada que baixou o arquivo. Arquivo baixado até JANELA_DA_PROVA antes da
# planilha e não alterado depois dela = a leitura é DESTE conteúdo. Medido em
# 19/10: 1.486 arquivos a menos de 10 min da planilha, 34 a mais de 1 dia (a
# planilha foi regravada sem download, ex.: --reler-telas) e nenhum no meio.
JANELA_DA_PROVA = 30 * 60
FOLGA_DO_RELOGIO = 2


def leitura_valida(leitura) -> bool:
    """Leitura que pode ir para o cache: dict vindo da IA, sem 'erro', com os
    campos mínimos do prompt, o tipo identificado e nada fora do formato."""
    if not isinstance(leitura, dict) or "erro" in leitura:
        return False
    if any(campo not in leitura for campo in CAMPOS_MINIMOS_DA_LEITURA):
        return False
    tipo = leitura.get("tipo_identificado")
    if not isinstance(tipo, str) or not tipo.strip() or tipo in LEITURAS_SEM_IA:
        return False
    if not isinstance(leitura.get("legivel"), bool):
        return False
    if not isinstance(leitura.get("confianca_campos"), (dict, type(None))):
        return False
    tabela = leitura.get("tabela_renda")
    return tabela is None or (isinstance(tabela, list) and all(isinstance(l, dict) for l in tabela))


def prova_da_planilha(caminho_arquivo: Path, caminho_xlsx: Path) -> bool:
    """True se o arquivo foi baixado na mesma rodada que gravou a planilha (até
    JANELA_DA_PROVA antes dela) e não mudou depois. Ver JANELA_DA_PROVA."""
    try:
        intervalo = caminho_xlsx.stat().st_mtime - Path(caminho_arquivo).stat().st_mtime
    except OSError:
        return False
    return -FOLGA_DO_RELOGIO <= intervalo <= JANELA_DA_PROVA


def hash_do_arquivo(caminho: Path) -> str | None:
    try:
        return hashlib.sha256(Path(caminho).read_bytes()).hexdigest()
    except OSError:
        return None


def _arquivo_do_cache(sha: str) -> Path:
    return PASTA_CACHE_IA / sha[:2] / f"{sha}.json"


def leitura_do_cache(caminho: Path) -> dict | None:
    """A leitura guardada para o conteúdo deste arquivo, ou None."""
    sha = hash_do_arquivo(caminho)
    if not sha:
        return None
    try:
        guardado = json.loads(_arquivo_do_cache(sha).read_text(encoding="utf-8"))
        leitura = guardado["leitura"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not leitura_valida(leitura):
        return None
    return dict(leitura, _leitura_completa=True)


def guardar_no_cache(caminho: Path, leitura: dict, tipo_cv: str, origem: str,
                     sobrescrever: bool = True) -> bool:
    """Grava a leitura com a chave do conteúdo do arquivo. Escrita atômica (um
    .tmp só deste processo e os.replace): a varredura e o executor gravam, e a
    trava já os serializa, mas um leitor nunca vê JSON pela metade. Devolve
    True se gravou. 'sobrescrever=False' é o da carga inicial pelas planilhas:
    não passa por cima de uma leitura que já esteja lá."""
    if not leitura_valida(leitura):
        return False
    sha = hash_do_arquivo(caminho)
    if not sha:
        return False
    destino = _arquivo_do_cache(sha)
    if not sobrescrever and destino.exists():
        return False
    conteudo = json.dumps({
        "sha256": sha, "versao_prompt": VERSAO_PROMPT, "origem": origem, "tipo_cv": tipo_cv,
        "gravado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "leitura": {k: v for k, v in leitura.items() if not k.startswith("_")},
    }, ensure_ascii=False)
    temporario = destino.with_name(f"{destino.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporario.write_text(conteudo, encoding="utf-8")
    except OSError:  # cache é ajuda: disco cheio ou sem permissão não derruba a análise
        return False
    for _ in range(20):  # Windows: troca recusada enquanto outro processo lê o destino
        try:
            os.replace(temporario, destino)
            return True
        except PermissionError:
            time.sleep(0.05)
    temporario.unlink(missing_ok=True)
    return False


def _leituras_provadas_das_planilhas(contagem: dict) -> list[tuple[Path, dict, str]]:
    """(arquivo, leitura, tipo_cv) das planilhas de saidas/ com leitura válida
    E prova de que é do arquivo que está na pasta (prova_da_planilha)."""
    provadas = []
    for pasta in listar_pastas_de_cliente():
        caminhos_xlsx = list(pasta.glob("*.xlsx"))
        if not caminhos_xlsx:
            continue
        try:
            anteriores = ler_resultados_anteriores(caminhos_xlsx[0])
        except Exception:  # planilha aberta/corrompida: fica para a próxima
            continue
        for arquivo, r in anteriores.items():
            doc_ia = r.get("doc_ia") or {}
            if not doc_ia.get("_leitura_completa") or not leitura_valida(doc_ia):
                contagem["sem_leitura_completa"] += 1
                continue
            caminho = pasta / arquivo
            if not caminho.exists():
                contagem["sem_arquivo"] += 1
                continue
            if not prova_da_planilha(caminho, caminhos_xlsx[0]):
                contagem["sem_prova"] += 1
                continue
            provadas.append((caminho, doc_ia, r.get("tipo_cv") or ""))
    return provadas


def alimentar_cache_com_as_planilhas() -> dict:
    """Carga inicial: as leituras das planilhas de saidas/ que são válidas e
    PROVADAS do arquivo ao lado delas vão para o cache (origem 'planilha') -
    para não pagar a IA de novo pelo que já foi lido. Não sobrescreve."""
    contagem = {"gravadas": 0, "ja_estavam": 0, "sem_arquivo": 0, "sem_leitura_completa": 0, "sem_prova": 0}
    for caminho, doc_ia, tipo_cv in _leituras_provadas_das_planilhas(contagem):
        if guardar_no_cache(caminho, doc_ia, tipo_cv, "planilha", sobrescrever=False):
            contagem["gravadas"] += 1
        else:
            contagem["ja_estavam"] += 1
    return contagem


def revisar_cache() -> dict:
    """Apaga do cache o que não passa nas regras novas: leitura inválida (de
    qualquer origem) e importação das planilhas (origem 'planilha') sem prova
    para aquele hash e aquela leitura. As leituras da IA válidas ficam."""
    contagem = {"sem_arquivo": 0, "sem_leitura_completa": 0, "sem_prova": 0}
    provadas: dict[str, set[str]] = {}
    for caminho, doc_ia, _ in _leituras_provadas_das_planilhas(contagem):
        sha = hash_do_arquivo(caminho)
        if sha:
            provadas.setdefault(sha, set()).add(json.dumps(
                {k: v for k, v in doc_ia.items() if not k.startswith("_")}, sort_keys=True, ensure_ascii=False))
    resultado = {"ficaram_ia": 0, "ficaram_planilha": 0, "apagadas_invalidas": 0, "apagadas_sem_prova": 0}
    for arquivo in list(PASTA_CACHE_IA.rglob("*.json")):
        try:
            guardado = json.loads(arquivo.read_text(encoding="utf-8"))
            leitura, origem, sha = guardado["leitura"], guardado.get("origem"), guardado["sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            leitura, origem, sha = None, None, None
        if not leitura_valida(leitura):
            motivo = "apagadas_invalidas"
        elif origem == "planilha" and json.dumps(leitura, sort_keys=True, ensure_ascii=False) not in provadas.get(sha, ()):
            motivo = "apagadas_sem_prova"
        else:
            resultado["ficaram_ia" if origem == "ia" else "ficaram_planilha"] += 1
            continue
        arquivo.unlink(missing_ok=True)
        resultado[motivo] += 1
    return resultado


# --------------------------------------------------------------------------
# Preparar o arquivo pra mandar pra IA
# --------------------------------------------------------------------------

# Quantas páginas de um PDF são enviadas pra IA, no máximo.
#
# Isto era 1 até 09/09/2026, e essa era uma falha silenciosa séria: na reserva
# 33773 (financiamento CEF real) a tabela RENDA da Tela de Avaliação de Risco
# fica na PÁGINA 2, então a IA nunca via a renda e devolvia renda_encontrada
# null. Não era erro de leitura nem do prompt - o dado simplesmente não era
# enviado. O limite existe pra um anexo gigante (contrato de 50 páginas) não
# estourar custo; 5 cobre com folga os documentos da cartilha.
MAX_PAGINAS_PDF_IA = 5


def pdf_paginas_para_png_bytes(
    caminho_pdf: Path, dpi: int = 150, max_paginas: int = MAX_PAGINAS_PDF_IA
) -> list[bytes]:
    """Converte as primeiras `max_paginas` páginas do PDF em PNG, uma imagem
    por página, na ordem. Devolve lista vazia se o PDF não tiver página."""
    import fitz  # pymupdf

    doc = fitz.open(caminho_pdf)
    try:
        total = min(len(doc), max_paginas)
        paginas = []
        for i in range(total):
            pix = doc.load_page(i).get_pixmap(dpi=dpi)
            paginas.append(pix.tobytes("png"))
        return paginas
    finally:
        doc.close()


def preparar_conteudo_para_ia(caminho_arquivo: Path) -> list[tuple[str, bytes]] | None:
    """Retorna uma LISTA de (media_type, bytes crus) pronta pro Gemini - uma
    entrada por página, no caso de PDF; uma entrada só, no caso de imagem.
    Devolve None se o tipo de arquivo não for suportado (ex: .docx, .zip)."""
    ext = caminho_arquivo.suffix.lower()

    if ext == ".pdf":
        paginas = pdf_paginas_para_png_bytes(caminho_arquivo)
        if not paginas:
            return None
        return [("image/png", dados) for dados in paginas]

    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        media_type = mimetypes.guess_type(str(caminho_arquivo))[0] or "image/jpeg"
        return [(media_type, caminho_arquivo.read_bytes())]

    return None  # .docx, .zip, .xlsx anexado etc. - fora do escopo do v1.1


# --------------------------------------------------------------------------
# Chamada à IA
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Consumo real da IA (21/10/2026): uma linha por chamada ao Gemini e por acerto
# do cache em painel_dados/consumo_ia-AAAA-MM.jsonl (ver painel/consumo.py).
# Sem dado pessoal: só o número da reserva (tirado do nome da pasta) e o TIPO
# do documento no CV, de uma lista fechada. A origem é marcada por quem roda:
# a varredura, o executor (reanálise) e a releitura das Telas trocam ORIGEM_DO_CONSUMO.
# --------------------------------------------------------------------------
PASTA_CONSUMO_IA = PASTA_SCRIPT / "painel_dados"
ORIGEM_DO_CONSUMO = "outro"

# Falhas da IA (21/10/2026, desenho da Turing; ver painel/ia_falhas.py). Falha de
# SISTEMA não é achado sobre o documento: ele fica "aguardando leitura" (aba
# ABA_FALHAS_IA), fora do cache e da aba de divergências, e a próxima rodada o lê
# de novo. O disjuntor (ARQUIVO_ESTADO_IA) é olhado antes de CADA chamada.
ARQUIVO_ESTADO_IA = PASTA_CONSUMO_IA / ia_falhas.NOME_DO_ARQUIVO
ESPERAS_DO_RATE_LIMIT_S = (5, 20)  # 429 por minuto: duas novas tentativas, e só
_dormir = time.sleep
# 429 sem marcador reconhecido (revisão da Turing): sem nova tentativa; N seguidos
# no mesmo processo (a rodada) armam o disjuntor como "indeterminado". Uma
# chamada que deu certo, ou que falhou por outro motivo, zera a contagem.
_seguidos_429 = 0
_trava_429 = threading.Lock()


def _armar_disjuntor(classe: str, lock_print: threading.Lock | None = None) -> None:
    """Arma e NUNCA ignora o retorno: sem conseguir gravar o ia_estado.json, o
    bloqueio fica em memória até o fim deste processo, e o log diz isso."""
    if ia_falhas.armar(ARQUIVO_ESTADO_IA, classe):
        return
    with lock_print or contextlib.nullcontext():
        print(f"  ATENÇÃO: disjuntor da IA ({classe}) NÃO foi gravado em {ARQUIVO_ESTADO_IA.name}; "
              f"as chamadas ficam bloqueadas em memória até o fim deste processo")


def _contar_429(falha: "ia_falhas.FalhaIA | None", lock_print: threading.Lock | None = None) -> None:
    global _seguidos_429
    if falha is not None and falha.classe == "bloqueada":
        return
    with _trava_429:
        _seguidos_429 = _seguidos_429 + 1 if falha is not None and falha.classe == "indeterminado_429" else 0
        armar = _seguidos_429 >= ia_falhas.SEGUIDOS_PARA_ARMAR_INDETERMINADO
    if armar:
        _armar_disjuntor("indeterminado", lock_print)


def _idreserva_da_pasta(caminho: Path) -> int | None:
    m = re.search(r"-reserva-(\d+)$", Path(caminho).name)
    return int(m.group(1)) if m else None


def registrar_consumo_ia(caminho_arquivo: Path, tipo_cv: str, **campos) -> None:
    """Nunca levanta exceção (consumo.registrar já engole tudo; isto é a 2ª rede)."""
    try:
        consumo_ia.registrar(PASTA_CONSUMO_IA, origem=ORIGEM_DO_CONSUMO,
                             idreserva=_idreserva_da_pasta(Path(caminho_arquivo).parent), tipo_cv=tipo_cv,
                             **campos)
    except Exception:
        pass


def extrair_dados_do_documento(client, modelo: str, caminho_arquivo: Path, tipo_cv: str) -> dict:
    """Lê o documento com a IA. Cada chamada fica registrada no consumo (tokens
    do usage_metadata, páginas, erro), inclusive a que falhou.

    O que é do DOCUMENTO (tipo não suportado, arquivo que não abre) volta como
    {"erro": ...} e vira divergência, como sempre. O que é do SISTEMA levanta
    ia_falhas.FalhaIA: disjuntor armado (nem chama), 429, rede, configuração e
    resposta inválida."""
    try:
        conteudo = preparar_conteudo_para_ia(caminho_arquivo)
    except Exception as exc:  # PDF corrompido etc.: problema do arquivo, não da IA
        return {"erro": f"Não foi possível abrir o arquivo para a leitura: {type(exc).__name__}"}
    if conteudo is None:
        return {"erro": f"Tipo de arquivo não suportado pela leitura de IA: {caminho_arquivo.suffix}"}
    for tentativa in range(len(ESPERAS_DO_RATE_LIMIT_S) + 1):
        bloqueio = ia_falhas.armado(ARQUIVO_ESTADO_IA)
        if bloqueio:
            raise ia_falhas.FalhaIA("bloqueada", f"{bloqueio['estado']} {bloqueio.get('classe') or ''}: "
                                                 f"{bloqueio.get('motivo')}")
        try:
            resp = _chamar_a_ia(client, modelo, caminho_arquivo, tipo_cv, conteudo)
            break
        except Exception as exc:
            registrar_consumo_ia(caminho_arquivo, tipo_cv, paginas=len(conteudo), modelo=modelo, erro=True)
            falha = ia_falhas.classificar(exc)
            if falha.classe != "rate_limit" or tentativa == len(ESPERAS_DO_RATE_LIMIT_S):
                raise falha from exc
            _dormir(ESPERAS_DO_RATE_LIMIT_S[tentativa])
    resultado = _interpretar_resposta(resp)
    registrar_consumo_ia(caminho_arquivo, tipo_cv, paginas=len(conteudo), modelo=modelo,
                         erro="erro" in resultado, uso=getattr(resp, "usage_metadata", None))
    if "erro" in resultado:  # só o motivo: o texto da resposta pode ter dado pessoal
        raise ia_falhas.FalhaIA("resposta_invalida", resultado["erro"].split(":", 1)[0])
    return resultado


def _chamar_a_ia(client, modelo: str, caminho_arquivo: Path, tipo_cv: str, conteudo: list):
    from google.genai import types

    prompt = PROMPT_EXTRACAO.format(nome_arquivo=caminho_arquivo.name, tipo_cv=tipo_cv or "não informado")

    # Documento de várias páginas: avisa a IA explicitamente, senão ela tende a
    # tratar cada imagem como um documento solto. A tabela RENDA da Tela de
    # Avaliação de Risco, por exemplo, fica na página 2.
    partes: list = []
    if len(conteudo) > 1:
        partes.append(
            f"Este documento tem {len(conteudo)} páginas, enviadas a seguir na ordem. "
            f"Trate TODAS como um único documento: informações importantes (por "
            f"exemplo a tabela RENDA) podem estar em qualquer uma delas."
        )
    for media_type, dados_bin in conteudo:
        partes.append(types.Part.from_bytes(data=dados_bin, mime_type=media_type))
    partes.append(prompt)

    return client.models.generate_content(
        model=modelo,
        contents=partes,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )


def _interpretar_resposta(resp) -> dict:
    # Resposta cortada pelo limite de tokens: leitura parcial, não vale (nem para o cache).
    motivo_fim = getattr((getattr(resp, "candidates", None) or [None])[0], "finish_reason", None)
    if getattr(motivo_fim, "name", str(motivo_fim)) == "MAX_TOKENS":
        return {"erro": "Resposta da IA cortada pelo limite de tokens"}

    texto = (resp.text or "").strip()
    # às vezes o modelo devolve com ```json ... ``` em volta - remove se tiver
    texto_limpo = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)

    try:
        lido = json.loads(texto_limpo)
    except json.JSONDecodeError:
        return {"erro": f"Resposta da IA não veio em JSON válido: {texto[:300]}"}
    if not isinstance(lido, dict):
        return {"erro": f"Resposta da IA não veio como objeto JSON: {texto[:300]}"}
    return lido


# --------------------------------------------------------------------------
# Confronto: documento (IA) vs cadastro (Excel)
# --------------------------------------------------------------------------

def montar_endereco_cadastro(cadastro: dict) -> str:
    """Junta os campos de endereço da aba 'Dados Cadastrais' numa string só,
    pra comparar com o que a IA leu no comprovante."""
    campos = ("endereco", "numero", "complemento", "bairro", "cidade", "estado", "cep")
    partes = [str(cadastro.get(c)).strip() for c in campos if cadastro.get(c)]
    return ", ".join(partes)


def cpf_valido(cpf: str) -> bool:
    """Valida CPF pelo dígito verificador (algoritmo oficial). Usado só pra
    diferenciar 'a IA/OCR provavelmente errou um dígito na leitura' de 'o
    CPF realmente é outro' - um CPF com dígito verificador inválido é sinal
    de erro de leitura, não necessariamente de fraude ou divergência real."""
    cpf = re.sub(r"\D", "", cpf or "")
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
    d1 = (soma * 10 % 11) % 10
    if d1 != int(cpf[9]):
        return False
    soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
    d2 = (soma * 10 % 11) % 10
    return d2 == int(cpf[10])


# Erro 3 de 20/10/2026: numa CNH a IA às vezes põe em 'cpf_encontrado' o Nº de
# Registro (ou o RG do campo DOC. IDENTIDADE) e a regra acusava "CPF diferente".
# Só some quando é o MESMO número que a IA identificou como registro
# ('registro_cnh', do prompt de 20/10). Número que não é CPF válido e é bem
# diferente do CPF do cadastro continua AVISO (pode ser a CNH de outra pessoa
# com o CPF mal lido), com um texto que diz o que provavelmente é.


def eh_cnh(doc_ia: dict) -> bool:
    tipo = str(doc_ia.get("tipo_identificado") or "").lower()
    return "cnh" in tipo or "habilita" in tipo


def cpf_lido_e_o_registro_da_cnh(doc_ia: dict, cpf_doc: str) -> bool:
    registro = re.sub(r"\D", "", str(doc_ia.get("registro_cnh") or ""))
    return eh_cnh(doc_ia) and bool(registro) and cpf_doc == registro


def numero_da_cnh_que_nao_parece_cpf(doc_ia: dict, cpf_doc: str, cpf_cad: str) -> bool:
    """CNH, número que não é CPF válido e com mais de 2 dígitos diferentes do
    CPF do cadastro (1 ou 2 trocados é o CPF lido errado: aviso de sempre)."""
    diferentes = sum(a != b for a, b in zip(cpf_doc, cpf_cad)) + abs(len(cpf_doc) - len(cpf_cad))
    return eh_cnh(doc_ia) and not cpf_valido(cpf_doc) and diferentes > 2


def confianca_do_campo(doc_ia: dict, campo: str) -> str:
    """Lê o nível de confiança que a IA reportou pra um campo (novo formato
    do prompt, com 'confianca_campos'). Documentos processados com uma
    versão antiga do script (sem esse campo) caem no default 'alta', pra
    não quebrar leitura de resultados já salvos - só passam a valer o novo
    comportamento cauteloso depois de reprocessados com --forcar."""
    return (doc_ia.get("confianca_campos") or {}).get(campo) or "alta"


# A partir de que diferença a renda do documento conta como divergente.
#
# Eram R$ 1,00 fixos até 23/09/2026, e isso era apertado demais: na reserva
# 33974 o documento trazia R$ 3.500,00 contra R$ 3.466,66 no cadastro - R$
# 33,34, menos de 1% - e virava divergência capaz de reprovar um contrato.
# Diferença desse tamanho é arredondamento, mês de referência diferente ou
# centavos de folha de pagamento, não erro de cadastro.
#
# A régua agora é a MAIOR entre um piso fixo e um percentual, pra funcionar
# tanto em renda baixa quanto alta. Calibrada contra os casos reais:
#   33974: 3.500,00 x 3.466,66 -> R$   33,34  (~1%)   NÃO acusa
#   33773: 6.731,69 x 4.010,57 -> R$ 2.721,12 (~68%)  acusa
#   33905: 3.919,34 x 1.636,23 -> R$ 2.283,11 (~140%) acusa
TOLERANCIA_RENDA_PISO_REAIS = 50.00
TOLERANCIA_RENDA_PERCENTUAL = 0.01  # 1% da renda cadastrada


def tolerancia_de_renda(renda_cadastrada: float) -> float:
    """Folga aceitável, em reais, pra renda desse tamanho."""
    return max(TOLERANCIA_RENDA_PISO_REAIS, abs(renda_cadastrada) * TOLERANCIA_RENDA_PERCENTUAL)


def parse_valor_monetario(valor) -> float | None:
    """Converte 'R$ 4.010,57', '4.010,57', '4010.57' ou 4010.57 em float.
    Devolve None se não der pra interpretar - nesse caso o chamador
    simplesmente não compara, em vez de inventar um número."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return float(valor)

    texto = re.sub(r"[^\d,.\-]", "", str(valor).strip())
    if not texto:
        return None

    # Formato brasileiro ("4.010,57") x americano ("4,010.57"): quem vier por
    # último manda - se a vírgula está depois do ponto, ela é o decimal.
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3},\d{3}", texto):
        # Uma vírgula seguida de exatamente 3 dígitos é milhar: "3,500" é
        # 3.500 reais, não 3,50 (revisão da Forja, 15/10/2026). Centavo tem 1
        # ou 2 dígitos ("3,5" e "3,50" continuam 3,50).
        texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):
        # Só ponto, em grupos de 3: é milhar no formato brasileiro. "R$ 3.500"
        # virava 3,50 reais e "1.234.567" não virava nada (14/10/2026).
        texto = texto.replace(".", "")

    try:
        return float(texto)
    except ValueError:
        return None


def formatar_reais(valor: float) -> str:
    """4010.57 -> '4.010,57' (formato brasileiro). Feito na mão porque
    depender de locale pt_BR instalado na máquina é frágil."""
    return f"{valor:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def parse_data(valor) -> date | None:
    """Aceita 'AAAA-MM-DD' (formato que o prompt pede) e 'DD/MM/AAAA' (o que
    a IA às vezes devolve mesmo assim). Devolve None se não reconhecer."""
    if valor is None:
        return None
    if isinstance(valor, date):
        return valor

    texto = str(valor).strip()
    if not texto:
        return None

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", texto)
    if m:
        ano, mes, dia = (int(g) for g in m.groups())
    else:
        m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", texto)
        if not m:
            return None
        dia, mes, ano = (int(g) for g in m.groups())

    try:
        return date(ano, mes, dia)
    except ValueError:
        return None


def parse_mes(valor) -> date | None:
    """'MM/AAAA', 'AAAA-MM' ou qualquer data que parse_data() aceite -> o dia 1
    daquele mês. Devolve None se não reconhecer."""
    texto = str(valor or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{4})$", texto) or re.match(r"^(\d{4})-(\d{1,2})$", texto)
    if m:
        a, b = (int(g) for g in m.groups())
        ano, mes = (b, a) if a <= 12 and b > 12 else (a, b)
        try:
            return date(ano, mes, 1)
        except ValueError:
            return None
    dia = parse_data(texto)
    return dia.replace(day=1) if dia else None


def validade_do_comprovante(mes: date) -> date:
    """Último dia do mês M+3 (gestora comercial, 14/10/2026): 01/2026 -> 30/04/2026,
    11/2025 -> 28/02/2026."""
    indice = mes.year * 12 + (mes.month - 1) + 4  # primeiro dia de M+4
    primeiro_de_m4 = date(indice // 12, indice % 12 + 1, 1)
    return date.fromordinal(primeiro_de_m4.toordinal() - 1)


def mes_do_comprovante(doc_ia: dict) -> tuple[date | None, str, str | None]:
    """(mês M, de onde ele veio, texto lido) para a validade do comprovante de
    endereço. Ver a ordem no comentário da regra, em
    comparar_documento_com_cadastro(). Sem data nenhuma: (None, "", None); só
    datas em formato que não reconheço: (None, "", primeiro texto lido)."""
    ilegivel = None
    for campo, origem in (("mes_referencia", "referência"),
                          ("data_referencia", "referência"),
                          ("data_emissao", "emissão")):
        texto = doc_ia.get(campo)
        if not texto:
            continue
        # O prompt manda repetir a emissão em 'data_referencia' quando o
        # documento tem data de emissão: aí a origem é a emissão.
        if campo == "data_referencia" and texto == doc_ia.get("data_emissao"):
            origem = "emissão"
        mes = parse_mes(texto)
        if mes is not None:
            return mes, origem, str(texto)
        ilegivel = ilegivel or str(texto)
    return None, "", ilegivel


def cadastro_e_pessoa_juridica(cadastro: dict) -> bool:
    """O titular da reserva é uma EMPRESA?

    Importa porque numa venda PJ os documentos pessoais anexados são do
    REPRESENTANTE da empresa - e a cartilha exige exatamente isso (contrato
    social, cartão CNPJ, pesquisa cadastral e os documentos do representante).

    Sem esta checagem o sistema comparava o CPF do representante contra o CNPJ
    da empresa e o nome dele contra a razão social, gerando duas divergências
    garantidas em toda venda PJ. Caso real (22/09/2026): o RG do representante
    foi acusado de não bater com a razão social da empresa compradora.
    """
    if str(cadastro.get("cliente_tipo") or "").strip().upper() == "J":
        return True
    if str(cadastro.get("documento_tipo") or "").strip().lower() == "cnpj":
        return True
    documento = re.sub(r"\D", "", str(cadastro.get("documento") or ""))
    return len(documento) == 14


def encontrar_associado(nome: str, cpf: str, associados: list[dict] | None) -> dict | None:
    """O nome/CPF lido no documento pertence a OUTRA pessoa da operação
    (2º comprador, avalista, cônjuge)?

    Se pertence, o documento não está errado - ele é de um co-participante
    legítimo da venda, e comparar com o cadastro do titular é comparar duas
    pessoas diferentes.

    Isso só ficou possível depois de 22/09/2026, quando descobrimos que
    GET /v1/cvdw/reservas/associados entrega essa lista sem precisar de senha.

    Casos reais que motivaram: na reserva 33974 a Tela de Aprovação traz
    o nome de um co-comprador e na 33905 o de outro -
    ambos com CPF válido, ambos acusados de "não bater com o cadastro" quando
    na verdade são os co-compradores.
    """
    if not associados:
        return None

    cpf_doc = re.sub(r"\D", "", cpf or "")
    if cpf_doc:
        for a in associados:
            if re.sub(r"\D", "", str(a.get("documento") or "")) == cpf_doc:
                return a

    nome_doc = (nome or "").strip().upper()
    if not nome_doc:
        return None
    for a in associados:
        nome_assoc = str(a.get("nome") or "").strip().upper()
        if not nome_assoc:
            continue
        try:
            from rapidfuzz import fuzz
            score = max(
                fuzz.token_sort_ratio(nome_assoc, nome_doc),
                fuzz.partial_ratio(nome_assoc, nome_doc),
            )
        except ImportError:
            score = 100 if nome_assoc == nome_doc else 0
        if score >= 85:
            return a
    return None


def comparar_documento_com_cadastro(
    doc_ia: dict, cadastro: dict, tipo_cv: str, associados: list[dict] | None = None,
) -> dict:
    """Retorna {"divergencias": [...], "avisos": [...]}.

    'associados' são as outras pessoas da reserva (2º comprador, avalista).
    Documento que pertence a uma delas não gera divergência de nome/CPF - vira
    aviso identificando de quem é. Parâmetro opcional pra não quebrar quem
    chama a função sem essa informação."""
    divergencias: list[str] = []
    avisos: list[str] = []

    if "erro" in doc_ia:
        divergencias.append(f"Não foi possível ler o documento: {doc_ia['erro']}")
        return {"divergencias": divergencias, "avisos": avisos}

    if doc_ia.get("legivel") is False:
        divergencias.append("Documento ilegível/de má qualidade segundo a IA")

    tipo_lower = (tipo_cv or "").lower()
    # cuidado: "endere" pega só "endereço"/"Endereço". Não usar "comprovante"
    # sozinho aqui, porque "Comprovante de Renda" também contém essa palavra
    # e não tem nada a ver com as regras abaixo.
    eh_comprovante_endereco = "endere" in tipo_lower

    if eh_comprovante_endereco:
        # Pra comprovante de endereço, o que importa é o ENDEREÇO bater com
        # o cadastro - não o nome de quem está na conta (a empresa aceita
        # comprovante em nome de pai/mãe, então checar nome geraria alarme
        # falso o tempo todo). Nome só é comparado nos outros tipos de
        # documento (RG, CNH, certidão etc.) mais abaixo.
        endereco_cad = montar_endereco_cadastro(cadastro)
        endereco_doc = str(doc_ia.get("endereco_encontrado") or "").strip()
        if endereco_cad and endereco_doc:
            try:
                from rapidfuzz import fuzz
                score = max(
                    fuzz.token_set_ratio(endereco_cad.upper(), endereco_doc.upper()),
                    fuzz.partial_ratio(endereco_cad.upper(), endereco_doc.upper()),
                )
            except ImportError:
                score = 100 if endereco_cad.upper() == endereco_doc.upper() else 0
            if score < 55:
                msg = (
                    f"Endereço no comprovante ('{endereco_doc}') não bate com o endereço cadastrado "
                    f"('{endereco_cad}') - similaridade {score:.0f}%"
                )
                if confianca_do_campo(doc_ia, "endereco_encontrado") == "baixa":
                    # IA já avisou que teve dificuldade pra ler esse campo -
                    # pode ser erro de leitura, não divergência real. Vira
                    # aviso (revisar manualmente) em vez de bloquear sozinho.
                    avisos.append(msg + " - ATENÇÃO: IA reportou baixa confiança nessa leitura, revisar manualmente")
                else:
                    divergencias.append(msg)
        elif not endereco_doc:
            divergencias.append("Não foi possível identificar o endereço no comprovante")
        elif not endereco_cad:
            avisos.append("Cadastro sem endereço preenchido pra comparar com o comprovante")

        # Validade: o comprovante do mês M vale até o último dia do mês M+3
        # (gestora comercial, 14/10/2026; janeiro vale até 30/04, novembro até o fim de
        # fevereiro). Substitui os 90 dias corridos de 22/09/2026. M é o mês de
        # REFERÊNCIA da conta; sem referência, o mês de emissão. A conta é
        # feita contra a data da conferência (hoje).
        #
        # Conta de consumo NÃO TEM data de emissão - tem mês de referência,
        # vencimento e data de leitura (correção de 23/09/2026: 4 de 5 reservas
        # validadas acusavam "sem data de emissão"). Por isso a ordem abaixo:
        #   1. 'mes_referencia' (MM/AAAA), campo próprio desde 14/10/2026;
        #   2. 'data_referencia', das leituras anteriores - é o mês de referência
        #      OU, na falta dele, o vencimento da conta (o prompt antigo juntava
        #      os dois; com o vencimento a conta sai um mês mais folgada);
        #   3. 'data_emissao'.
        mes_doc, origem_data, data_str = mes_do_comprovante(doc_ia)
        if data_str and mes_doc is None:
            divergencias.append(f"Data do comprovante em formato inesperado: {data_str}")
        elif mes_doc is not None:
            valido_ate = validade_do_comprovante(mes_doc)
            if date.today() > valido_ate:
                divergencias.append(
                    f"Comprovante de endereço vencido: {origem_data} {mes_doc:%m/%Y}, "
                    f"válido até {valido_ate:%d/%m/%Y}"
                )
            elif mes_doc > date.today():
                avisos.append(
                    f"Data do comprovante está no futuro ({origem_data} {mes_doc:%m/%Y}) - "
                    f"comum em conta com vencimento à frente, conferir"
                )
        else:
            divergencias.append(
                "Comprovante de endereço sem nenhuma data que permita conferir a validade"
            )

    elif cadastro_e_pessoa_juridica(cadastro):
        # Venda PJ: os documentos pessoais anexados são do REPRESENTANTE da
        # empresa, e a cartilha exige exatamente isso. Comparar o CPF dele com
        # o CNPJ da empresa, ou o nome dele com a razão social, gera duas
        # divergências garantidas e sem sentido - foi o que aconteceu numa
        # venda PJ real. Registra quem é e segue.
        nome_doc = str(doc_ia.get("nome_encontrado") or "").strip()
        if nome_doc:
            avisos.append(
                f"Cliente é pessoa jurídica ('{cadastro.get('nome')}'), e este documento é de "
                f"'{nome_doc}' - representante da empresa, conforme a cartilha. Nome e CPF não "
                f"foram comparados com o cadastro (comparar pessoa com empresa não faz sentido)"
            )

        cnpj_cad = re.sub(r"\D", "", str(cadastro.get("documento") or ""))
        cnpj_doc = re.sub(r"\D", "", str(doc_ia.get("cnpj_encontrado") or ""))

        # Conferir o TAMANHO antes de comparar. CNPJ tem 14 dígitos; qualquer
        # coisa diferente disso não é CNPJ, é a IA tendo lido outro número como
        # se fosse. Caso real no mesmo dia em que este bloco foi escrito
        # (22/09/2026): numa venda PJ a IA extraiu "12345678" como CNPJ na
        # certidão - eram os 8 dígitos do CEP do endereço - e o
        # código acusou divergência de CNPJ num documento que não tem CNPJ.
        if cnpj_doc and len(cnpj_doc) != 14:
            avisos.append(
                f"IA extraiu '{cnpj_doc}' como CNPJ, mas isso não tem 14 dígitos - "
                f"provável leitura de outro número (CEP, inscrição, protocolo). Ignorado"
            )
        elif cnpj_cad and cnpj_doc and cnpj_cad != cnpj_doc:
            divergencias.append(
                f"CNPJ no documento ({cnpj_doc}) difere do cadastro ({cnpj_cad})"
            )

    else:
        # Nos outros tipos de documento (RG, CPF, CNH, certidão etc.) o nome
        # e o CPF continuam sendo comparados normalmente.
        nome_cad = str(cadastro.get("nome") or "").strip()
        nome_doc = str(doc_ia.get("nome_encontrado") or "").strip()
        if nome_cad and nome_doc:
            try:
                from rapidfuzz import fuzz
                # token_sort_ratio penaliza forte quando o documento tem MAIS
                # nomes que o cadastro (ex: certidão de casamento com o casal
                # inteiro) ou usa abreviações (ex: "Fulana Ap G de Tal").
                # partial_ratio acha o nome do cadastro "dentro" do texto do
                # documento, então usamos o melhor dos dois pra evitar esses
                # falsos positivos.
                score = max(
                    fuzz.token_sort_ratio(nome_cad.upper(), nome_doc.upper()),
                    fuzz.partial_ratio(nome_cad.upper(), nome_doc.upper()),
                )
            except ImportError:
                score = 100 if nome_cad.upper() == nome_doc.upper() else 0
            if score < 75:
                msg = f"Nome no documento ('{nome_doc}') difere do cadastro ('{nome_cad}') - similaridade {score:.0f}%"
                associado = encontrar_associado(nome_doc, doc_ia.get("cpf_encontrado") or "", associados)
                if associado:
                    avisos.append(
                        f"Documento é de {associado.get('nome')} "
                        f"({associado.get('tipo_associacao', 'associado')}), não de "
                        f"{nome_cad} - é co-participante legítimo da operação, não divergência"
                    )
                elif confianca_do_campo(doc_ia, "nome_encontrado") == "baixa":
                    avisos.append(msg + " - ATENÇÃO: IA reportou baixa confiança nessa leitura, revisar manualmente")
                else:
                    divergencias.append(msg)

        cpf_cad = re.sub(r"\D", "", str(cadastro.get("documento") or ""))
        cpf_doc = re.sub(r"\D", "", str(doc_ia.get("cpf_encontrado") or ""))
        if cpf_lido_e_o_registro_da_cnh(doc_ia, cpf_doc):
            cpf_doc = ""  # o Nº de Registro da CNH não é o CPF: não há CPF lido para comparar
        if cpf_cad and cpf_doc and cpf_cad != cpf_doc:
            msg = f"CPF no documento ({cpf_doc}) difere do cadastro ({cpf_cad})"
            # Se o CPF que a IA leu no documento nem passa na validação de
            # dígito verificador, é sinal forte de erro de OCR/leitura (a
            # pessoa não anexaria um CPF inválido de propósito) - vira aviso
            # em vez de divergência, mesmo com confiança "alta" reportada.
            associado = encontrar_associado(doc_ia.get("nome_encontrado") or "", cpf_doc, associados)
            if associado:
                avisos.append(
                    f"CPF do documento pertence a {associado.get('nome')} "
                    f"({associado.get('tipo_associacao', 'associado')}), co-participante da operação - "
                    f"não é divergência de cadastro"
                )
            elif numero_da_cnh_que_nao_parece_cpf(doc_ia, cpf_doc, cpf_cad):
                avisos.append(f"Número lido na CNH ({cpf_doc}) não é um CPF válido (pode ser o Nº de registro "
                              f"ou o RG) - conferir o CPF da CNH com o do cadastro ({cpf_cad})")
            elif not cpf_valido(cpf_doc):
                avisos.append(msg + " - ATENÇÃO: o CPF lido no documento não é válido (dígito verificador não bate), provável erro de leitura, revisar manualmente")
            elif confianca_do_campo(doc_ia, "cpf_encontrado") == "baixa":
                avisos.append(msg + " - ATENÇÃO: IA reportou baixa confiança nessa leitura, revisar manualmente")
            else:
                divergencias.append(msg)

    # ----------------------------------------------------------------------
    # Renda: aqui só a TRAVA DE TIPO DE DOCUMENTO (22/09/2026).
    #
    # A regra da gestora comercial é que a ÚNICA fonte válida de renda é a Tela de Avaliação
    # de Risco / Cadastro CEF. A regra 10 do prompt manda a IA deixar
    # 'renda_encontrada' null em qualquer outro documento, mas na reserva 33937
    # ela preencheu 18.216,00 lendo uma Declaração de Ajuste Anual, e o sistema
    # sugeriu trocar a renda cadastrada (100.000,00) por um valor ANUAL de
    # imposto de renda. Instrução de prompt é probabilística: a trava é pelo
    # TIPO do campo no CV (dado fixo, não interpretação da IA).
    #
    # A COMPARAÇÃO com o CV saiu desta função em 14/10/2026 e virou
    # conferir_renda_da_pessoa(), que roda uma vez por pasta: a renda é conferida
    # POR PROPONENTE (gestora comercial, 14/10/2026), e a Tela de um proponente pode estar na
    # pasta de outro - um documento sozinho não tem como saber disso.
    # ----------------------------------------------------------------------
    renda_doc = parse_valor_monetario(doc_ia.get("renda_encontrada"))
    if renda_doc is not None and not documento_e_tela_de_risco(tipo_cv):
        # A IA extraiu renda de um documento que não é fonte válida. Não
        # compara, não sugere - só registra, porque é sinal de que o prompt
        # escorregou e alguém precisa saber disso.
        avisos.append(
            f"IA extraiu uma renda (R$ {formatar_reais(renda_doc)}) de um documento do tipo "
            f"'{tipo_cv}', que NÃO é fonte válida de renda (só Tela de Avaliação de Risco / "
            f"Cadastro CEF valem, conforme a cartilha). Valor IGNORADO na comparação"
        )

    # ----------------------------------------------------------------------
    # Validade vencida. Vale tanto pra validade da proposta (Tela de Avaliação
    # de Risco) quanto pra validade do próprio documento (ex: CNH) - a IA põe
    # os dois no mesmo campo 'data_validade'.
    #
    # EXCEÇÃO: certidão de estado civil NÃO VENCE. Confirmado pelo João em
    # 16/09/2026: "é igual uma certidão de nascimento". Sem essa trava, uma
    # data qualquer que a IA lesse no documento (data de registro, data de
    # emissão da 2ª via) podia ser interpretada como vencimento e gerar
    # divergência num documento que é válido pra sempre.
    # ----------------------------------------------------------------------
    if any(t in tipo_lower for t in TIPOS_SEM_PRAZO_DE_VALIDADE):
        return {"divergencias": divergencias, "avisos": avisos}

    # Comprovante de endereço tem REGRA PRÓPRIA (mês M vale até o fim de M+3,
    # aplicada mais acima) e não deve passar pela regra genérica de validade.
    #
    # Sem esta exceção o mesmo documento era reprovado DUAS VEZES: na reserva
    # 33937 o comprovante é um boleto, e a IA colocou a data de VENCIMENTO DO
    # BOLETO em 'data_validade' - gerando "Validade vencida em 08/05/2026" além
    # do correto "com 168 dias". Vencimento de boleto não é validade de
    # documento; é quando a conta vence. Contar as duas inflava o número de
    # divergências sem acrescentar informação (22/09/2026).
    if eh_comprovante_endereco:
        return {"divergencias": divergencias, "avisos": avisos}

    validade = parse_data(doc_ia.get("data_validade"))

    # Documento de identificação SEM validade legível não pode passar calado.
    # Confirmado pelo João em 22/09/2026 que documento de identificação vencido
    # TRAVA a validação do contrato. Com a regra bloqueando, "não consegui ler
    # a data" deixa de ser detalhe: sem isso, um documento vencido escaparia só
    # porque a IA ficou em dúvida - que é justamente o que o prompt manda ela
    # fazer quando há várias datas candidatas (caso real: a CNH da reserva
    # 33773 tem validade geral e validade por categoria, em anos diferentes).
    # Vira AVISO, não divergência: a dúvida é da leitura, não do documento.
    eh_documento_de_identificacao = any(
        termo_no_texto(t, tipo_lower) for t in DOCUMENTOS_COMUNS_TODAS_MODALIDADES[0]
    )
    if eh_documento_de_identificacao and validade is None:
        avisos.append(
            "Não foi possível confirmar a data de validade deste documento de "
            "identificação - conferir manualmente se está vigente (documento "
            "vencido impede a validação do contrato)"
        )

    if validade is not None and validade < date.today():
        msg = f"Validade vencida em {validade:%d/%m/%Y} (venceu há {(date.today() - validade).days} dia(s))"
        if confianca_do_campo(doc_ia, "data_validade") == "baixa":
            avisos.append(msg + " - ATENÇÃO: IA reportou baixa confiança nessa leitura, revisar manualmente")
        else:
            divergencias.append(msg)

    return {"divergencias": divergencias, "avisos": avisos}


# --------------------------------------------------------------------------
# Ler o Excel gerado pelo mvp_v1_validador.py
# --------------------------------------------------------------------------

def ler_dados_cadastrais(caminho_xlsx: Path) -> dict:
    from openpyxl import load_workbook

    wb = load_workbook(caminho_xlsx, data_only=True)
    ws = wb["Dados Cadastrais"]
    dados = {}
    for linha in ws.iter_rows(min_row=2, values_only=True):
        if linha and linha[0]:
            dados[linha[0]] = linha[1]
    return dados


def ler_lista_documentos(caminho_xlsx: Path) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(caminho_xlsx, data_only=True)
    ws = wb["Documentos"]
    cabecalho = [c.value for c in ws[1]]
    docs = []
    for linha in ws.iter_rows(min_row=2, values_only=True):
        if not any(linha):
            continue
        docs.append(dict(zip(cabecalho, linha)))
    return docs


# Rótulos da coluna 'arquivo' nas linhas da aba "Divergencias (IA)" que NÃO são
# documento: são achados da pasta inteira, que gravar_aba_divergencias() escreve
# e ler_resultados_anteriores() tem que pular. Constantes compartilhadas para os
# dois lados não divergirem se alguém mudar o texto de um só.
ROTULO_DOCUMENTO_NAO_ANEXADO = "(documento não anexado)"
ROTULO_CONJUNTO_DA_PASTA = "(conjunto da pasta)"
ROTULOS_DE_SENTINELA = (ROTULO_DOCUMENTO_NAO_ANEXADO, ROTULO_CONJUNTO_DA_PASTA)


ERROS_DE_SISTEMA_ANTIGOS = ("Falha ao chamar a IA", "Resposta da IA", "Leitura da IA não veio")


def ler_resultados_anteriores(caminho_xlsx: Path) -> dict[str, dict]:
    """Lê a aba 'Divergencias (IA)' já existente (se tiver) e devolve um
    dict {arquivo: resultado}, no mesmo formato que processar_pasta_cliente
    monta pra cada documento novo. Usado pra não perder o que já foi lido
    quando só uns documentos são novos."""
    from openpyxl import load_workbook

    if not caminho_xlsx.exists():
        return {}

    wb = load_workbook(caminho_xlsx, data_only=True)
    if "Divergencias (IA)" not in wb.sheetnames:
        return {}

    ws = wb["Divergencias (IA)"]
    cabecalho = [c.value for c in ws[1]]
    resultados: dict[str, dict] = {}
    for linha in ws.iter_rows(min_row=2, values_only=True):
        if not any(linha):
            continue
        d = dict(zip(cabecalho, linha))
        arquivo = d.get("arquivo")
        if not arquivo:
            continue
        # Linha-sentinela não é leitura de documento: é a CONCLUSÃO da rodada
        # anterior sobre a pasta (documento ausente, arquivo repetido, campo
        # errado), e gravar_aba_divergencias() a regera do zero a cada execução.
        #
        # 09/10/2026, verificação offline do projeto. Elas entravam no cache como
        # se fossem documentos e eram regravadas junto com as novas. Ao corrigir
        # o casamento de 'rg' (mesmo dia), uma pasta reprocessada ficava com a
        # divergência nova "falta identificação" E o aviso velho "o documento
        # existe, campo errado" - lado a lado, contraditórios -, e a cada rodada
        # mais uma sentinela velha se acumulava. É a armadilha 7 ao contrário:
        # o cache guarda a LEITURA para a regra nova valer sobre ela, e não pode
        # deixar a conclusão da regra velha para trás.
        if arquivo in ROTULOS_DE_SENTINELA:
            continue
        divergencias_texto = str(d.get("divergencias") or "")
        avisos_texto = str(d.get("avisos") or "")
        # Planilha gerada a partir de 06/10/2026 traz a leitura inteira em
        # JSON: dá pra reaplicar as regras sem IA. As antigas só têm algumas
        # colunas soltas - a leitura fica INCOMPLETA e as regras NÃO podem ser
        # reaplicadas em cima dela (faltaria nome, CPF, endereço, e as
        # comparações sumiriam em silêncio, parecendo que o documento
        # melhorou). O marcador '_leitura_completa' é o que distingue os dois
        # casos, e quem reaplica as regras tem que respeitar ele.
        leitura: dict = {}
        bruto = d.get("leitura_ia_json")
        if bruto:
            try:
                leitura = json.loads(bruto)
                leitura["_leitura_completa"] = True
            except (ValueError, TypeError):
                leitura = {}
        if not leitura:
            leitura = {
                "tipo_identificado": d.get("tipo_identificado_ia") or "",
                "observacoes": d.get("observacoes_ia") or "",
                # Excel gerado antes de 09/09/2026 não tem nem essas colunas -
                # .get() devolve None e o resultado antigo segue válido.
                "renda_encontrada": d.get("renda_encontrada") or None,
                "data_validade": d.get("data_validade") or None,
            }
            leitura["_leitura_completa"] = False

        # Falha de SISTEMA gravada como divergência antes de 21/10/2026 ("Falha ao
        # chamar a IA: 429..."): não é leitura. Fica de fora e é lida de novo.
        if str(leitura.get("erro") or "").startswith(ERROS_DE_SISTEMA_ANTIGOS):
            continue
        resultados[arquivo] = {
            "arquivo": arquivo,
            "tipo_cv": d.get("tipo_cv") or "",
            "doc_ia": leitura,
            "divergencias": [] if divergencias_texto in ("", "OK") else divergencias_texto.split(" | "),
            "avisos": [] if not avisos_texto else avisos_texto.split(" | "),
        }
    return resultados


def detectar_arquivo_repetido(pasta: Path, documentos: list[dict]) -> list[str]:
    """O MESMO arquivo foi anexado em tipos de documento diferentes?

    Regra nova de 28/09/2026, motivada por um caso real: numa reserva de venda
    PJ os SEIS documentos eram o mesmo PDF, anexado como RG, como certidão de
    estado civil, como comprovante de endereço, como comprovante de renda,
    como CTPS e como simulação.

    Nenhuma das outras regras pega isso, e por um motivo estrutural: elas
    olham UM documento por vez, e cada documento isolado parece plausível. É o
    CONJUNTO que denuncia. É também exatamente o padrão de burla que a gestora comercial
    descreveu - preencher todo campo obrigatório pra destravar o processo.

    Compara por HASH do conteúdo, não por nome: renomear não escapa, e dois
    arquivos com nomes diferentes mas conteúdo idêntico são pegos. Cai pro
    nome original do CV quando não dá pra ler o conteúdo.

    É DIVERGÊNCIA independentemente de modalidade - diferente da regra de
    "documento não exigido vira aviso". Um documento não exigido pode estar
    ruim sem invalidar nada; o mesmo arquivo em vários campos é outra coisa,
    porque significa que os campos EXIGIDOS também foram preenchidos assim.
    """
    import hashlib

    por_conteudo: dict[str, list[str]] = {}

    for doc in documentos:
        tipo = (doc.get("tipo") or "").strip()
        nome_local = doc.get("arquivo_local")
        if not tipo or not nome_local:
            continue

        caminho = pasta / nome_local
        chave = None
        if caminho.exists():
            try:
                chave = hashlib.sha256(caminho.read_bytes()).hexdigest()
            except OSError:
                chave = None
        if chave is None:
            # Sem conteúdo legível, cai pro nome ORIGINAL do CV (doc["nome"]),
            # não pro nome local - o local já vem prefixado com o tipo, então
            # nunca se repetiria e a checagem nunca acharia nada.
            nome_cv = (doc.get("nome") or "").strip().lower()
            if not nome_cv:
                continue
            chave = f"nome:{nome_cv}"

        por_conteudo.setdefault(chave, []).append(tipo)

    divergencias: list[str] = []
    for chave, tipos in por_conteudo.items():
        tipos_distintos = sorted(set(tipos))
        if len(tipos_distintos) < 2:
            continue
        como = "mesmo nome de arquivo" if chave.startswith("nome:") else "conteúdo idêntico"
        divergencias.append(
            f"O MESMO arquivo ({como}) foi anexado em {len(tipos_distintos)} tipos de "
            f"documento diferentes: {', '.join(tipos_distintos)} - indica preenchimento dos "
            f"campos só para destravar o processo, não documentação real"
        )

    return divergencias


def gravar_aba_divergencias(
    caminho_xlsx: Path, resultados: list[dict], divergencias_ausencia: list[str] | None = None,
    divergencias_pasta: list[str] | None = None, avisos_pasta: list[str] | None = None,
    falha_associados: str | None = None,
    extras: list[tuple[str, str, str]] | None = None,
) -> None:
    """'extras' (14/10/2026): outros achados da pasta, como (rótulo,
    "divergencia" ou "aviso", mensagem) - a conferência de renda por proponente
    e a certidão do cônjuge (ver concluir_pasta())."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill

    wb = load_workbook(caminho_xlsx)
    if "Divergencias (IA)" in wb.sheetnames:
        del wb["Divergencias (IA)"]
    ws = wb.create_sheet("Divergencias (IA)")

    # 'renda_encontrada' e 'data_validade' viraram colunas em 09/09/2026: a IA
    # já extraía os dois desde o prompt reforçado, mas eles não apareciam em
    # lugar nenhum nem eram comparados - o trabalho de leitura era invisível.
    # Como o cache (ler_resultados_anteriores) casa por NOME de coluna, Excel
    # antigo sem essas colunas continua sendo lido normalmente.
    # 06/10/2026: a LEITURA da IA passou a ser gravada inteira, não só a
    # conclusão. Duas razões, nessa ordem de importância:
    #
    # 1. Regra nova passa a valer na base já processada SEM uma chamada de IA -
    #    processar_pasta_cliente reaplica aplicar_regras() sobre a leitura
    #    guardada. Antes só --forcar fazia efeito, e --forcar relê tudo.
    # 2. Quem abre a planilha consegue CONFERIR por que um documento passou ou
    #    não passou. Até aqui o nome, o CPF, o endereço e as datas que a IA leu
    #    não apareciam em lugar nenhum - o trabalho de leitura era invisível e
    #    o apontamento, inauditável.
    #
    # 'leitura_ia_json' guarda o dicionário completo (inclusive confianca_campos
    # e campos que o prompt venha a ganhar). As colunas antes dele são as mesmas
    # informações em forma legível, pra conferência humana.
    colunas = [
        "arquivo", "tipo_cv", "tipo_identificado_ia", "divergencias", "avisos",
        "nome_encontrado", "cpf_encontrado", "endereco_encontrado",
        "data_emissao", "data_referencia", "data_validade",
        "renda_encontrada", "legivel", "observacoes_ia", "leitura_ia_json",
    ]
    ws.append(colunas)
    for cel in ws[1]:
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = PatternFill("solid", fgColor="0E6070")

    # Consulta de associados que FALHOU (09/10/2026). Primeira linha, porque
    # muda a leitura de todas as outras: sem a lista, documento de
    # co-participante pode ter virado divergência de nome/CPF e a renda foi
    # comparada como se a operação tivesse uma pessoa só. Vai na coluna de
    # AVISOS, de propósito: é falha da ferramenta, não irregularidade da venda,
    # e não pode inflar a contagem de divergências. Como é linha-sentinela
    # (ROTULO_CONJUNTO_DA_PASTA), some sozinha na rodada que consultar com
    # sucesso - ler_resultados_anteriores() não a carrega no cache.
    if falha_associados:
        ws.append([ROTULO_CONJUNTO_DA_PASTA, "", "Associados não consultados", "",
                   f"A consulta de associados no CV falhou ({falha_associados}): documento "
                   f"de co-participante pode ter virado divergência de nome/CPF e a renda "
                   f"foi comparada como se a operação tivesse uma pessoa só. Rode de novo."]
                  + [""] * (len(colunas) - 5))
        for cel in ws[ws.max_row]:
            cel.fill = PatternFill("solid", fgColor="F7E3E5")

    # Documentos obrigatórios pela modalidade que nem foram anexados na
    # pasta - uma linha por documento ausente, sem "arquivo" (não existe
    # arquivo pra apontar) e sem terem passado pela IA.
    for msg in (divergencias_ausencia or []):
        ws.append([ROTULO_DOCUMENTO_NAO_ANEXADO, "", "Documento obrigatório ausente", msg]
                  + [""] * (len(colunas) - 4))
        for cel in ws[ws.max_row]:
            cel.fill = PatternFill("solid", fgColor="F7E3E5")

    # Achados que são da PASTA inteira, não de um documento isolado - hoje só
    # o "mesmo arquivo em vários tipos". Ficam no topo porque dizem mais sobre
    # a reserva do que qualquer linha individual.
    for msg in (divergencias_pasta or []):
        ws.append([ROTULO_CONJUNTO_DA_PASTA, "", "Mesmo arquivo em vários campos", msg]
                  + [""] * (len(colunas) - 4))
        for cel in ws[ws.max_row]:
            cel.fill = PatternFill("solid", fgColor="F7E3E5")

    # Documento exigido que ESTÁ anexado, só que num campo de outro nome. É
    # aviso, não divergência: a venda não está sem o documento.
    for msg in (avisos_pasta or []):
        ws.append([ROTULO_CONJUNTO_DA_PASTA, "", "Documento em campo errado", "", msg]
                  + [""] * (len(colunas) - 5))
        for cel in ws[ws.max_row]:
            cel.fill = PatternFill("solid", fgColor="FBF1D6")

    for rotulo, nivel, msg in (extras or []):
        if nivel == "divergencia":
            ws.append([ROTULO_CONJUNTO_DA_PASTA, "", rotulo, msg] + [""] * (len(colunas) - 4))
            cor = "F7E3E5"
        else:
            ws.append([ROTULO_CONJUNTO_DA_PASTA, "", rotulo, "", msg] + [""] * (len(colunas) - 5))
            cor = "FBF1D6"
        for cel in ws[ws.max_row]:
            cel.fill = PatternFill("solid", fgColor=cor)

    for r in resultados:
        divergencias_texto = " | ".join(r["divergencias"]) if r["divergencias"] else "OK"
        avisos_texto = " | ".join(r["avisos"]) if r["avisos"] else ""
        leitura = {k: v for k, v in r["doc_ia"].items() if not k.startswith("_")}
        ws.append([
            r["arquivo"],
            r["tipo_cv"],
            leitura.get("tipo_identificado", ""),
            divergencias_texto,
            avisos_texto,
            leitura.get("nome_encontrado") or "",
            leitura.get("cpf_encontrado") or "",
            leitura.get("endereco_encontrado") or "",
            leitura.get("data_emissao") or "",
            leitura.get("data_referencia") or "",
            leitura.get("data_validade") or "",
            leitura.get("renda_encontrada") if leitura.get("renda_encontrada") is not None else "",
            "" if leitura.get("legivel") is None else ("sim" if leitura.get("legivel") else "NÃO"),
            leitura.get("observacoes", ""),
            json.dumps(leitura, ensure_ascii=False),
        ])
        # vermelho = tem divergência de verdade (precisa agir).
        # amarelo = só aviso (ex: possível comprovante de pai/mãe - aceito,
        # mas vale confirmar visualmente).
        if r["divergencias"]:
            cor = "F7E3E5"
        elif r["avisos"]:
            cor = "FBF1D6"
        else:
            cor = None
        if cor:
            for cel in ws[ws.max_row]:
                cel.fill = PatternFill("solid", fgColor=cor)

    larguras = [28, 20, 20, 50, 45, 26, 14, 34, 13, 13, 13, 14, 9, 40, 14]
    from openpyxl.utils import get_column_letter
    for idx, largura in enumerate(larguras, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = largura

    wb.save(caminho_xlsx)


ABA_FALHAS_IA = "Falhas da IA"
COLUNAS_FALHAS_IA = ("arquivo", "tipo_cv", "classe", "mensagem", "em")


def gravar_aba_falhas_ia(caminho_xlsx: Path, falhas: list[dict]) -> None:
    """Os documentos que ficaram AGUARDANDO LEITURA por falha da IA nesta
    rodada (ver painel/ia_falhas.py). Aba própria, nunca a de divergências: não
    é achado sobre a venda. Sem falha na rodada, a aba some."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill

    wb = load_workbook(caminho_xlsx)
    if ABA_FALHAS_IA in wb.sheetnames:
        del wb[ABA_FALHAS_IA]
    elif not falhas:
        return
    if falhas:
        ws = wb.create_sheet(ABA_FALHAS_IA)
        ws.append(list(COLUNAS_FALHAS_IA))
        for cel in ws[1]:
            cel.font = Font(bold=True, color="FFFFFF")
            cel.fill = PatternFill("solid", fgColor="0E6070")
        for r in falhas:
            f = r["falha_ia"]
            ws.append([r["arquivo"], r["tipo_cv"], f["classe"], f["mensagem"], f["em"]])
        for letra, largura in zip("ABCDE", (28, 20, 18, 50, 17)):
            ws.column_dimensions[letra].width = largura
    wb.save(caminho_xlsx)


# --------------------------------------------------------------------------
# Contexto da análise (13/10/2026)
#
# O painel (painel/) precisa filtrar por modalidade, empreendimento,
# imobiliária, corretor e situação, e nada disso ficava na pasta: a modalidade
# era consultada, usada e jogada fora. Sem a aba, o painel dependia das
# planilhas de amostra/varredura, que cobrem só parte das reservas. O registro
# da reserva JÁ está em memória em processar_pasta_cliente(), então gravar o
# contexto não custa nenhuma chamada nova ao CV.
# --------------------------------------------------------------------------

ABA_CONTEXTO = "Contexto da analise"
CAMPOS_CONTEXTO = ("modalidade", "tipovenda", "empreendimento", "unidade", "bloco", "etapa",
                   "imobiliaria", "corretor", "situacao", "analisado_em")
ROTULO_MODALIDADE = {
    MODALIDADE_FINANCIAMENTO: "Financiamento",
    MODALIDADE_VENDA_DIRETA: "Venda direta",
    MODALIDADE_LOTEAMENTO: "Loteamento",
}


def _desembrulhar_registro(registro) -> dict | None:
    """GET /v1/comercial/reservas/{id} devolve o registro dentro de uma chave
    "<idreserva>", e buscar_reserva_individual() o repassa assim. A listagem
    (/v1/cvdw/reservas) já vem plana."""
    if not isinstance(registro, dict):
        return None
    if len(registro) == 1:
        chave, valor = next(iter(registro.items()))
        if str(chave).isdigit() and isinstance(valor, dict):
            return valor
    return registro


def _texto_do_campo(valor) -> str:
    if valor is None or isinstance(valor, (dict, list)):
        return ""
    return str(valor).strip()


def contexto_da_reserva(registro: dict | None, modalidade: str | None) -> dict:
    """Campos de contexto a partir do registro da reserva, nos DOIS formatos do
    CV (ver campos-cv-reserva.md):
      - plano, da listagem: empreendimento, unidade, bloco, etapa, imobiliaria,
        corretor, situacao;
      - aninhado, da busca individual: unidade.{empreendimento, unidade, bloco,
        etapa}, imobiliaria.nome, corretor.corretor, situacao.situacao.
    Campo ausente = "". Sem registro (pré-cadastro que não virou reserva), só a
    modalidade - vazia -, para não gravar uma linha em branco por campo."""
    rotulo = ROTULO_MODALIDADE.get(modalidade, "")
    reg = _desembrulhar_registro(registro)
    if not reg:
        return {"modalidade": rotulo}

    def aninhado(nome: str, subcampo: str) -> str:
        valor = reg.get(nome)
        return _texto_do_campo(valor.get(subcampo) if isinstance(valor, dict) else valor)

    unidade = reg.get("unidade")
    if isinstance(unidade, dict):
        local = {c: _texto_do_campo(unidade.get(c)) for c in ("empreendimento", "unidade", "bloco", "etapa")}
    else:
        local = {c: _texto_do_campo(reg.get(c)) for c in ("empreendimento", "unidade", "bloco", "etapa")}

    return {
        "modalidade": rotulo,
        "tipovenda": _texto_do_campo(reg.get("tipovenda")),
        **local,
        "imobiliaria": aninhado("imobiliaria", "nome"),
        "corretor": aninhado("corretor", "corretor"),
        "situacao": aninhado("situacao", "situacao"),
    }


def gravar_aba_contexto(caminho_xlsx: Path, contexto: dict) -> None:
    """(Re)escreve a aba "Contexto da analise" (Campo/Valor), nos campos de
    CAMPOS_CONTEXTO que vierem em 'contexto', mais 'analisado_em' (agora, se não
    vier). A aba é apagada e recriada: rodar de novo não duplica linha."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill

    valores = dict(contexto)
    valores.setdefault("analisado_em", datetime.now().strftime("%d/%m/%Y %H:%M"))

    wb = load_workbook(caminho_xlsx)
    if ABA_CONTEXTO in wb.sheetnames:
        del wb[ABA_CONTEXTO]
    ws = wb.create_sheet(ABA_CONTEXTO)
    ws.append(["Campo", "Valor"])
    for cel in ws[1]:
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = PatternFill("solid", fgColor="0E6070")
    for campo in CAMPOS_CONTEXTO:
        if campo in valores:
            ws.append([campo, _texto_do_campo(valores[campo])])
            # Texto do CV começando com "=" não pode virar fórmula.
            if str(ws.cell(ws.max_row, 2).value or "").startswith("="):
                ws.cell(ws.max_row, 2).data_type = "s"
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 50
    wb.save(caminho_xlsx)


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------

def aplicar_regras(
    doc_ia: dict, cadastro: dict, tipo_cv: str, modalidade: str | None,
    associados: list[dict] | None = None, papel: str | None = None,
) -> tuple[list[str], list[str]]:
    """TODAS as regras aplicadas sobre UMA leitura de documento. Devolve
    (divergencias, avisos). 'papel' é o dono da pasta (ver
    documento_e_exigido()); None = titular.

    Separada do _processar_um_documento em 06/10/2026, e é a mudança de
    arquitetura mais importante do projeto até aqui. Antes, ler com IA e
    aplicar regra eram a mesma função, e o cache guardava a CONCLUSÃO - então:

      - regra nova não fazia efeito em documento já lido, só com --forcar;
      - e --forcar relê tudo com IA, o que SORTEIA a leitura de novo.

    O segundo é o problema de verdade. Na reserva 34428, entre duas rodadas do
    MESMO arquivo, a IA mudou de "Carteira de Trabalho Digital e Holerites"
    para "Carteira de Trabalho Digital" e inventou uma validade vencida que
    não estava na rodada anterior. Misturando as duas coisas, fica impossível
    saber se o relatório mudou por causa da regra nova ou porque a IA leu
    diferente - ou seja, TODA mudança de regra vira inverificável. E este
    projeto vive de mudar regra.

    Com a leitura guardada e as regras aplicadas sempre:
      - regra nova vale na base inteira de graça, sem uma chamada de IA;
      - rodar duas vezes dá o mesmo resultado.
    """
    resultado = comparar_documento_com_cadastro(doc_ia, cadastro, tipo_cv, associados)
    divergencias = list(resultado["divergencias"])
    avisos = list(resultado["avisos"])

    # O arquivo é mesmo do tipo do campo? Entra ANTES do rebaixamento abaixo
    # de propósito: num campo que a modalidade não exige, a troca de documento
    # vira aviso sozinha, como todo o resto.
    tipo_conferido = checar_tipo_do_documento(
        tipo_cv, doc_ia.get("tipo_identificado", "")
    )
    divergencias += tipo_conferido["divergencias"]
    avisos += tipo_conferido["avisos"]

    # Recalibração de 22/09/2026. Se este documento NÃO é exigido pela
    # modalidade, nada nele pode reprovar o contrato - a cartilha não pede o
    # documento, então um defeito nele não invalida a venda. As divergências
    # viram avisos, com o motivo escrito junto pra ninguém achar que sumiram.
    if divergencias and not documento_e_exigido(tipo_cv, modalidade, papel):
        avisos = avisos + [
            f"{msg} - OBS: '{tipo_cv}' não é documento exigido nesta modalidade, "
            f"então isto NÃO reprova a validação (registrado só pra conferência)"
            for msg in divergencias
        ]
        divergencias = []

    return divergencias, avisos


# Sentinelas gravadas no lugar da leitura da IA quando o documento nem chega a
# ser lido. Não passam por aplicar_regras - não há leitura pra aplicar regra
# em cima, e a conclusão delas é fixa.
LEITURAS_SEM_IA = ("Fora do escopo de validação", "Dispensado pela modalidade",
                   "Documento obrigatório ausente", "Mesmo arquivo em vários campos")


def _processar_um_documento(
    client, modelo: str, pasta: Path, doc: dict, cadastro: dict, indice: int, total: int,
    lock_print: threading.Lock, modalidade: str | None = None,
    associados: list[dict] | None = None, leitura_pronta: dict | None = None,
) -> dict | None:
    """Roda numa thread: lê UM documento com a IA e compara com o cadastro.
    Nunca deixa uma exceção subir e derrubar as outras threads - se a IA falha
    (rede, limite, saldo...), o resultado sai com 'falha_ia' e SEM divergência
    (ver _resultado_de_falha) e os outros continuam normalmente."""
    nome_arquivo = doc["arquivo_local"]
    caminho_arquivo = pasta / nome_arquivo
    tipo_cv = doc.get("tipo", "")

    if fora_do_escopo_de_validacao(tipo_cv):
        # CTPS (e outros tipos dessa lista) não fazem parte do escopo de
        # validação do contrato - nem manda pra IA, nem pode virar
        # divergência. Ver TIPOS_FORA_DO_ESCOPO_DE_VALIDACAO.
        with lock_print:
            print(f"  [{indice}/{total}] {nome_arquivo}: fora do escopo de validação ({tipo_cv}) - não analisado")
        return {
            "arquivo": nome_arquivo,
            "tipo_cv": tipo_cv,
            "doc_ia": registro_fora_do_escopo(tipo_cv),
            "divergencias": [],
            "avisos": [],
        }

    motivo_dispensa = dispensado_pela_modalidade(tipo_cv, modalidade)
    if motivo_dispensa:
        # Documento existe e foi anexado, mas essa modalidade não exige
        # ele (ex: Tela de Avaliação de Risco em Loteamento/Venda Direta) -
        # não manda pra IA (economiza custo), não pode virar divergência,
        # mesmo que o conteúdo esteja em branco/errado.
        with lock_print:
            print(f"  [{indice}/{total}] {nome_arquivo}: dispensado pela modalidade ({tipo_cv}) - não analisado")
        return {
            "arquivo": nome_arquivo,
            "tipo_cv": tipo_cv,
            "doc_ia": {
                "tipo_identificado": "Dispensado pela modalidade",
                "observacoes": motivo_dispensa,
            },
            "divergencias": [],
            "avisos": [],
        }

    if not caminho_arquivo.exists():
        with lock_print:
            print(f"  [{indice}/{total}] {nome_arquivo}: arquivo não encontrado na pasta, pulando")
        return None

    if leitura_pronta is not None:  # do cache persistente: nenhuma chamada à IA
        doc_ia = leitura_pronta
        registrar_consumo_ia(caminho_arquivo, tipo_cv, do_cache=True)  # a economia, medida
    else:
        try:
            doc_ia = extrair_dados_do_documento(client, modelo, caminho_arquivo, doc.get("tipo", ""))
        except Exception as exc:  # falha de SISTEMA: fica aguardando leitura, fora do cache
            falha = ia_falhas.classificar(exc)
            with lock_print:
                print(f"  [{indice}/{total}] {nome_arquivo}: aguardando leitura - {falha.mensagem}"
                      + (f" [{falha.detalhe}]" if falha.detalhe else ""))
            if falha.classe == "limite_ou_saldo":
                _armar_disjuntor("limite_ou_saldo", lock_print)
            _contar_429(falha, lock_print)
            return _resultado_de_falha(nome_arquivo, tipo_cv, falha)
        _contar_429(None)
        guardar_no_cache(caminho_arquivo, doc_ia, tipo_cv, "ia")
    # 34660 (19/10/2026): num PDF com duas certidões, a IA devolveu uma LISTA
    # (uma leitura por certidão) e o .get() das regras derrubava a pasta inteira.
    # extrair_dados_do_documento() já recusa o que não é objeto; aqui é a última
    # barreira para qualquer origem: vira falha de leitura só deste documento.
    if not isinstance(doc_ia, dict):
        return _resultado_de_falha(nome_arquivo, tipo_cv, ia_falhas.FalhaIA(
            "resposta_invalida", f"leitura não é objeto ({type(doc_ia).__name__})"))

    # O papel vem do próprio cadastro da pasta, de onde processar_pasta_cliente()
    # também o lê - ver documento_e_exigido() (13/10/2026).
    divergencias, avisos = aplicar_regras(
        doc_ia, cadastro, tipo_cv, modalidade, associados,
        cadastro.get("papel_na_operacao"),
    )

    with lock_print:
        if divergencias:
            status = f"{len(divergencias)} divergência(s)"
        elif avisos:
            status = f"{len(avisos)} aviso(s)"
        else:
            status = "OK"
        print(f"  [{indice}/{total}] {nome_arquivo}: {status}"
              + (" (do cache)" if leitura_pronta is not None else ""))

    return {
        "arquivo": nome_arquivo,
        "tipo_cv": doc.get("tipo", ""),
        "doc_ia": doc_ia,
        "divergencias": divergencias,
        "avisos": avisos,
    }


def _resultado_de_falha(nome_arquivo: str, tipo_cv: str, falha: "ia_falhas.FalhaIA") -> dict:
    """Documento AGUARDANDO LEITURA: nenhuma divergência, nenhum aviso, nenhuma
    leitura. processar_pasta_cliente() o tira dos resultados e o grava só na aba
    ABA_FALHAS_IA; o detalhe técnico fica só no log."""
    return {"arquivo": nome_arquivo, "tipo_cv": tipo_cv, "doc_ia": {}, "divergencias": [], "avisos": [],
            "falha_ia": {"classe": falha.classe, "mensagem": falha.mensagem,
                         "em": datetime.now().strftime("%d/%m/%Y %H:%M")}}


def _ler_com_a_ia(client, modelo: str, paralelismo: int, pasta: Path, para_ia: list[dict],
                  cadastro: dict, lock_print: threading.Lock, modalidade: str | None,
                  associados: list[dict]) -> list[dict]:
    """No máximo 'paralelismo' leituras em voo, e o disjuntor é olhado antes de
    CADA submissão: armado no meio da pasta, o que ainda não foi submetido nem
    chega a chamar a IA e fica aguardando leitura."""
    resultados: list[dict] = []
    fila = list(enumerate(para_ia, start=1))
    nao_submetidos = 0
    bloqueio = visto = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=paralelismo) as executor:
        em_voo: set = set()
        while fila or em_voo:
            while fila and len(em_voo) < paralelismo:
                i, doc = fila.pop(0)
                bloqueio = ia_falhas.armado(ARQUIVO_ESTADO_IA)
                if bloqueio:
                    visto = bloqueio
                    if (pasta / doc["arquivo_local"]).exists():
                        nao_submetidos += 1
                        resultados.append(_resultado_de_falha(doc["arquivo_local"], doc.get("tipo", ""),
                                                              ia_falhas.FalhaIA("bloqueada")))
                    continue
                em_voo.add(executor.submit(
                    _processar_um_documento, client, modelo, pasta, doc, cadastro, i, len(para_ia),
                    lock_print, modalidade, associados))
            if not em_voo:
                break
            prontos, em_voo = concurrent.futures.wait(em_voo, return_when=concurrent.futures.FIRST_COMPLETED)
            for futuro in prontos:
                resultado = futuro.result()
                if resultado is not None:
                    resultados.append(resultado)
    if nao_submetidos:
        with lock_print:
            print(f"  disjuntor da IA {visto['estado']} ({visto.get('motivo')}): "
                  f"{nao_submetidos} documento(s) nem foram enviados - "
                  f"aguardando leitura (libere com: python -m painel ia-liberar)")
    return resultados


def reaplicar_regras_no_cache(
    anteriores: dict[str, dict], cadastro: dict, modalidade: str | None,
    associados: list[dict] | None,
) -> dict:
    """Recalcula divergências e avisos dos documentos JÁ LIDOS, usando a
    leitura guardada e as regras de HOJE. Altera `anteriores` no lugar.

    Só mexe em quem tem a leitura COMPLETA gravada (planilha de 06/10/2026 em
    diante). Recalcular em cima de uma leitura parcial faria as comparações de
    nome, CPF e endereço simplesmente não acontecerem, e divergências reais
    sumiriam em silêncio - o documento pareceria ter melhorado. Isso seria bem
    pior do que manter o resultado antigo, então os incompletos ficam como
    estão e o usuário é avisado.
    """
    mudaram = 0
    incompletos = 0
    # Dono da pasta, para documento_e_exigido() (13/10/2026). Lido do cadastro,
    # como em processar_pasta_cliente(), para não mudar a assinatura.
    papel = cadastro.get("papel_na_operacao")
    for resultado in anteriores.values():
        doc_ia = resultado.get("doc_ia") or {}
        tipo_identificado = str(doc_ia.get("tipo_identificado") or "")

        # Documento que nem passou pela IA (fora do escopo, dispensado pela
        # modalidade): conclusão fixa, nada a recalcular.
        if tipo_identificado in LEITURAS_SEM_IA:
            continue

        # Tipo que SAIU do escopo depois de lido (Simulação CCA, 14/10/2026):
        # a leitura guardada não pode continuar gerando aviso, mas ela FICA.
        # Na reserva 34323 a Tela de Avaliação de Risco estava anexada no campo
        # Simulação CCA; apagar a leitura transformava "Tela no campo errado"
        # (aviso) em "Tela ausente" (divergência) - medido em 14/10/2026.
        tipo_cv = resultado.get("tipo_cv") or ""
        if fora_do_escopo_de_validacao(tipo_cv):
            if resultado["divergencias"] or resultado["avisos"]:
                mudaram += 1
            resultado["divergencias"] = []
            resultado["avisos"] = []
            continue

        if not doc_ia.get("_leitura_completa"):
            incompletos += 1
            continue

        divergencias, avisos = aplicar_regras(
            doc_ia, cadastro, resultado.get("tipo_cv") or "",
            modalidade, associados, papel,
        )
        if divergencias != resultado["divergencias"] or avisos != resultado["avisos"]:
            mudaram += 1
        resultado["divergencias"] = divergencias
        resultado["avisos"] = avisos

    return {"mudaram": mudaram, "incompletos": incompletos}


# --------------------------------------------------------------------------
# A OPERAÇÃO inteira (14/10/2026)
#
# Cada pessoa da reserva tem a sua pasta (mvp_v2_reserva.py), e até aqui cada
# pasta era julgada sozinha. Duas regras da gestora comercial (14/10/2026) não cabem nisso:
#   - a renda é conferida POR PROPONENTE, e a Tela de Avaliação de Risco pode
#     vir uma com todos (anexada num grupo só) ou uma por proponente;
#   - a certidão de casamento de um casal é o mesmo papel para os dois.
# Por isso a pasta passa a enxergar as IRMÃS: as outras pastas da mesma
# reserva, lidas do disco (planilha), sem nenhuma chamada nova ao CV ou à IA.
# --------------------------------------------------------------------------

ROTULO_RENDA = "Conferência de renda"
ROTULO_CERTIDAO_CONJUGE = "Certidão do cônjuge"


def documento_e_tela_de_risco(tipo_cv: str) -> bool:
    """O CAMPO do CV é o da Tela de Avaliação de Risco? É o campo, e não a
    leitura da IA, que decide se um documento é fonte de renda (trava de
    22/09/2026, reserva 33937)."""
    tipo_lower = (tipo_cv or "").lower()
    return any(t in tipo_lower for t in TERMOS_TELA_DE_RISCO)


def ler_pastas_irmas(pasta: Path, documentos_da_operacao: set[str] | None) -> list[dict]:
    """As outras pastas da mesma reserva ('...-reserva-{id}' na mesma pasta-mãe),
    cada uma como {"pasta", "cadastro", "tipos_presentes", "resultados"}.

    'documentos_da_operacao' são os CPFs/CNPJs (só dígitos) de quem participa
    da operação HOJE. Pasta de alguém que saiu da reserva fica no disco de
    rodadas antigas e não pode emprestar Tela nem certidão; por isso, sem essa
    lista (pré-cadastro, consulta de associados que falhou), nenhuma irmã é
    usada. Pasta que não abre é pulada - irmã é ajuda, não requisito."""
    m = re.search(r"(-reserva-\d+)$", pasta.name)
    if not m or not documentos_da_operacao:
        return []
    irmas = []
    for outra in sorted(pasta.parent.glob(f"*{m.group(1)}")):
        if outra == pasta or not outra.is_dir():
            continue
        xs = list(outra.glob("*.xlsx"))
        if not xs:
            continue
        try:
            cadastro = ler_dados_cadastrais(xs[0])
            if re.sub(r"\D", "", str(cadastro.get("documento") or "")) not in documentos_da_operacao:
                continue
            irmas.append({
                "pasta": outra,
                "cadastro": cadastro,
                "tipos_presentes": [d.get("tipo", "") for d in ler_lista_documentos(xs[0])],
                "resultados": list(ler_resultados_anteriores(xs[0]).values()),
            })
        except Exception as exc:  # planilha aberta no Excel, corrompida...
            print(f"  (pasta irmã {outra.name} não pôde ser lida: {exc} - seguindo sem ela)")
    return irmas


def rotulo_da_pessoa(papel: str | None) -> str:
    return "do titular" if papel_e_titular(papel) else f"do {papel}"


def telas_lidas(resultados: list[dict]) -> list[dict]:
    """Leituras da IA dos documentos no campo de Tela de Avaliação de Risco."""
    telas = []
    for r in resultados:
        doc_ia = r.get("doc_ia") or {}
        if (documento_e_tela_de_risco(r.get("tipo_cv") or "")
                and str(doc_ia.get("tipo_identificado") or "") not in LEITURAS_SEM_IA
                and "erro" not in doc_ia):
            telas.append(doc_ia)
    return telas


def linhas_de_renda(tela: dict) -> list[tuple[str, float | None, str]] | None:
    """(CPF, renda, coluna) de cada linha da tabela RENDA da Tela. A renda é a
    BRUTA; com a bruta em branco, a LÍQUIDA - na Tela da 34386 (releitura de
    14/10/2026) a bruta vinha "-" e a líquida era exatamente a renda do CV, nas
    duas linhas. Linha sem nenhuma das duas fica com renda None. Devolve None
    quando a leitura é anterior a 14/10/2026 e não traz a tabela (só
    'renda_encontrada', de uma linha sem CPF)."""
    tabela = tela.get("tabela_renda")
    if not isinstance(tabela, list):
        return None
    linhas = []
    for linha in tabela:
        if not isinstance(linha, dict):
            continue
        cpf = re.sub(r"\D", "", str(linha.get("cpf") or ""))
        if not cpf:
            continue
        bruta = parse_valor_monetario(linha.get("renda_bruta"))
        liquida = parse_valor_monetario(linha.get("renda_liquida"))
        if bruta is not None:
            linhas.append((cpf.zfill(11), bruta, "bruta"))
        elif liquida is not None:
            linhas.append((cpf.zfill(11), liquida, "líquida"))
        else:
            linhas.append((cpf.zfill(11), None, ""))
    return linhas


def conferir_renda_da_pessoa(
    cpf: str, papel: str | None, renda_cv, telas: list[dict], operacao_de_uma_pessoa: bool,
) -> tuple[list[str], list[str]]:
    """Confere a renda DESTA pessoa no CV contra a linha dela na tabela RENDA de
    qualquer Tela de Avaliação de Risco da operação. Devolve (divergencias,
    avisos).

    gestora comercial, 14/10/2026: o setor de contratos não apura renda; só confere se a
    renda do CV bate com a Tela, POR PROPONENTE. A renda do CV é a da pessoa:
    'renda_cliente_principal' do pré-cadastro para o titular, 'renda_familiar'
    do registro de associado para os demais (os dois somam o 'renda_total' do
    pré-cadastro - medido em 33859: 4.987,84 + 3.920,00 = 8.907,84).

    Até 14/10/2026 a comparação era a de UMA linha da Tela (sem saber de quem)
    contra o total do núcleo, e por isso só rodava em operação de uma pessoa: nas
    outras, ~47% das reservas, a renda não era conferida (aviso "lida na Tela
    (uma linha da tabela) não foi comparada"). A tolerância continua a de
    23/09/2026 (tolerancia_de_renda()).

    Sem CPF na linha não há como saber de quem é a renda, então leitura antiga
    (sem 'tabela_renda') só é comparada em operação de UMA pessoa, como antes."""
    rotulo = rotulo_da_pessoa(papel)
    if not telas:
        return [], []
    renda_pessoa = parse_valor_monetario(renda_cv)
    cpf = re.sub(r"\D", "", cpf or "").zfill(11)

    achados: list[tuple[float, dict, str]] = []
    alguma_tabela = False
    leitura_antiga = False
    linha_sem_valor = False
    for tela in telas:
        linhas = linhas_de_renda(tela)
        if linhas is None:
            leitura_antiga = True
            renda_unica = parse_valor_monetario(tela.get("renda_encontrada"))
            if operacao_de_uma_pessoa and renda_unica is not None:
                achados.append((renda_unica, tela, "bruta"))
            continue
        alguma_tabela = True
        for cpf_linha, renda, coluna in linhas:
            if cpf_linha != cpf:
                continue
            if renda is None:
                linha_sem_valor = True
            else:
                achados.append((renda, tela, coluna))

    if not achados:
        if linha_sem_valor:
            return [], [
                f"A linha {rotulo} na tabela RENDA da Tela de Avaliação de Risco não traz "
                f"renda legível (bruta nem líquida) - renda não conferida, conferir"
            ]
        if leitura_antiga and not operacao_de_uma_pessoa:
            return [], [
                f"Tela de Avaliação de Risco lida antes de 14/10/2026, sem a tabela RENDA "
                f"completa: a renda {rotulo} não foi conferida por proponente. Releia as "
                f"Telas (python analisar_documentos.py <reserva> --reler-telas)"
            ]
        if alguma_tabela and renda_pessoa:
            return [], [
                f"Renda {rotulo} no CV é R$ {formatar_reais(renda_pessoa)}, mas o CPF desta "
                f"pessoa não aparece na tabela RENDA de nenhuma Tela de Avaliação de Risco "
                f"lida na operação - conferir"
            ]
        return [], []

    if renda_pessoa is None:
        return [], [
            f"Não consegui a renda {rotulo} no CV para comparar com a Tela de Avaliação "
            f"de Risco (R$ {formatar_reais(achados[0][0])}) - conferir manualmente"
        ]
    if renda_pessoa == 0:
        return [], [
            f"Renda {rotulo} na Tela de Avaliação de Risco é R$ {formatar_reais(achados[0][0])}, "
            f"mas o CV não tem renda para esta pessoa - conferir"
        ]

    # Mais de uma Tela com a linha desta pessoa (uma com todos E uma dela, ou
    # Tela antiga e nova): basta uma bater. A que mais se aproxima é a relatada.
    renda_tela, tela, coluna = min(achados, key=lambda a: abs(a[0] - renda_pessoa))
    diferenca = renda_tela - renda_pessoa
    if abs(diferenca) <= tolerancia_de_renda(renda_pessoa):
        return [], []
    pct = abs(diferenca) / renda_pessoa * 100
    msg = (
        f"Renda {rotulo} na Tela de Avaliação de Risco (R$ {formatar_reais(renda_tela)}) difere "
        f"da renda no CV (R$ {formatar_reais(renda_pessoa)}) - diferença de "
        f"R$ {formatar_reais(abs(diferenca))} ({pct:.0f}%) {'a mais' if diferenca > 0 else 'a menos'} "
        f"na Tela"
        + (" (renda líquida: a bruta está em branco na Tela)" if coluna == "líquida" else "")
    )
    campo = "tabela_renda" if "tabela_renda" in tela else "renda_encontrada"
    if confianca_do_campo(tela, campo) == "baixa":
        return [], [msg + " - ATENÇÃO: IA reportou baixa confiança nessa leitura, revisar manualmente"]
    return [msg], []


def _e_casado(cadastro: dict) -> bool:
    # O cadastro da pessoa traz o código (1 = casado, conferido contra o texto
    # "Casado(a) - Regime ..." do registro da reserva em 33828, 34386, 34559).
    ec = str(cadastro.get("estado_civil") or "").strip().lower()
    return ec == "1" or ec.startswith("casad")


# Partículas que não contam como nome ("Maria DA Silva"). Sem elas um "DOS"
# passaria por sobrenome.
PARTICULAS_DE_NOME = {"DE", "DA", "DO", "DAS", "DOS", "E", "DI", "DU", "DEL"}


def _tokens_de_nome(texto: str) -> list[str]:
    return [t for t in re.split(r"[^A-Z]+", _sem_acento(texto).upper()) if t]


def nomes_no_texto(texto: str) -> list[list[str]]:
    """Os nomes próprios INTEIROS de um texto livre da IA, cada um como lista de
    partes (sem acento, maiúsculas, sem partículas). Um nome é uma sequência de
    palavras com inicial maiúscula ("FULANA CRISTINA" ou "Fulana Cristina"),
    com partículas no meio ("da", "dos"); termina em pontuação, em palavra
    minúscula ou no "e"/"E" que junta dois nomes ("BELTRANO SOUZA E FULANA
    ..."). Recortar o nome inteiro é o que impede "MARIA SILVA" de casar dentro
    de "ANA MARIA SILVA SANTOS"."""
    nomes: list[list[str]] = []
    atual: list[str] = []
    for palavra in re.findall(r"[^\W\d_]+|[^\w\s]", str(texto or "")):
        limpa = _sem_acento(palavra).upper()
        if palavra[0].isalpha() and limpa in PARTICULAS_DE_NOME and limpa != "E":
            if atual:
                atual.append(limpa)
            continue
        if palavra[0].isalpha() and palavra[0].isupper() and limpa != "E":
            atual.append(limpa)
            continue
        if atual:
            nomes.append(atual)
        atual = []
    if atual:
        nomes.append(atual)
    return [[p for p in n if p not in PARTICULAS_DE_NOME] for n in nomes if n]


def nome_aparece_no_texto(nome: str, texto: str) -> bool:
    """O nome COMPLETO da pessoa aparece no texto da leitura?

    Rígido de propósito (revisão da Forja, 15/10/2026): um "contém" frouxo
    (partial_ratio) deixava "MARIA SILVA" casar dentro de "ANA MARIA SILVA
    SANTOS", e com isso a certidão de uma pessoa servir para outra. Exige:
      - o nome tem ao menos 2 partes com mais de 2 letras (fora partículas);
      - um dos nomes inteiros do texto (nomes_no_texto()) COMEÇA no primeiro
        nome e TERMINA no último sobrenome da pessoa, exatamente, com no máximo
        2 partes a mais (nome de solteira / de casada);
      - cada parte do nome aparece nele (tolerando um erro de leitura, ratio
        >= 85) e o conjunto bate (token_set_ratio >= 90)."""
    from rapidfuzz import fuzz

    partes = [t for t in _tokens_de_nome(nome) if t not in PARTICULAS_DE_NOME]
    if len([t for t in partes if len(t) > 2]) < 2:
        return False
    for candidato in nomes_no_texto(texto):
        if (candidato and candidato[0] == partes[0] and candidato[-1] == partes[-1]
                and len(candidato) <= len(partes) + 2
                and all(any(fuzz.ratio(p, x) >= 85 for x in candidato) for p in partes)
                and fuzz.token_set_ratio(" ".join(partes), " ".join(candidato)) >= 90):
            return True
    return False


def certidao_do_conjuge(cadastro: dict, irmas: list[dict]) -> str | None:
    """Se outra pessoa da operação é cônjuge DESTA e tem a certidão de casamento
    lida na pasta dela, devolve o papel dela (texto para a mensagem); senão None.

    gestora comercial, 14/10/2026: casados entre si, a certidão é a mesma para os dois.

    "Casados entre si" NÃO sai do 'idconjuge' do associado: medido em 14/10/2026
    nas 31 linhas de associado das reservas de saidas/, ele é sempre igual ao
    próprio 'idpessoa' - não aponta para o cônjuge. Então vale só com prova
    dupla: as duas pessoas casadas no cadastro E o nome completo desta pessoa na
    leitura da certidão de casamento da outra (nome_aparece_no_texto()). A mesma
    'data_casamento' nos dois cadastros também valia até 15/10/2026 e saiu na
    revisão da Forja: data igual não prova que um é casado com o outro. Na
    dúvida, None - e a certidão ausente continua apontada."""
    if not _e_casado(cadastro):
        return None
    nome_proprio = str(cadastro.get("nome") or "").strip()
    if not nome_proprio:
        return None
    for irma in irmas:
        outro = irma["cadastro"]
        if not _e_casado(outro):
            continue
        for r in irma["resultados"]:
            leitura = r.get("doc_ia") or {}
            tipo_ia = str(leitura.get("tipo_identificado") or "")
            if ("estado civil" not in _sem_acento(r.get("tipo_cv"))
                    or "casamento" not in _sem_acento(tipo_ia)
                    or motivo_certidao_nao_vale(tipo_ia) is not None):
                continue
            texto = f"{leitura.get('nome_encontrado') or ''} | {leitura.get('observacoes') or ''}"
            if nome_aparece_no_texto(nome_proprio, texto):
                return outro.get("papel_na_operacao") or "titular"
    return None


def concluir_pasta(
    pasta: Path, cadastro: dict, documentos: list[dict], resultados: list[dict],
    modalidade: str | None, irmas: list[dict], renda_cv,
    operacao_de_uma_pessoa: bool,
) -> dict:
    """Os achados da PASTA (não de um documento), depois da leitura: ausência,
    arquivo repetido, campo errado, certidão do cônjuge e renda. Não lê nada
    do CV nem chama a IA - é o que permite medir regra nova offline."""
    papel = cadastro.get("papel_na_operacao")
    tipos_presentes = [d.get("tipo", "") for d in documentos]
    # A Tela é da operação (EXIGIDOS_UMA_VEZ_POR_OPERACAO) e pode ter sido
    # anexada no grupo de outra pessoa (gestora comercial, 14/10/2026). Só ela vem das irmãs.
    tipos_para_ausencia = tipos_presentes + [
        t for irma in irmas for t in irma["tipos_presentes"] if documento_e_tela_de_risco(t)
    ]
    faltam, campo_errado, avisos_conjuge = ausencias_apos_leitura(
        modalidade, papel, tipos_para_ausencia, resultados, certidao_do_conjuge(cadastro, irmas))

    extras: list[tuple[str, str, str]] = [
        (ROTULO_CERTIDAO_CONJUGE, "aviso", m) for m in avisos_conjuge]
    if not cadastro_e_pessoa_juridica(cadastro):
        telas = telas_lidas(resultados) + [
            t for irma in irmas for t in telas_lidas(irma["resultados"])]
        div_renda, av_renda = conferir_renda_da_pessoa(
            str(cadastro.get("documento") or ""), papel, renda_cv, telas, operacao_de_uma_pessoa)
        extras += [(ROTULO_RENDA, "divergencia", m) for m in div_renda]
        extras += [(ROTULO_RENDA, "aviso", m) for m in av_renda]

    return {
        "divergencias_ausencia": faltam,
        "avisos_campo_errado": campo_errado,
        "divergencias_pasta": detectar_arquivo_repetido(pasta, documentos),
        "extras": extras,
    }


def processar_pasta_cliente(
    pasta: Path, forcar: bool = False,
    associados_consultados: list[dict] | None = None, falha_associados: str | None = None,
    reler_telas: bool = False,
) -> None:
    """'associados_consultados' / 'falha_associados': a lista de associados da
    reserva (como vem do CV) e o motivo curto se a consulta falhou, já obtidos
    por quem chama (processar_reserva_completa(devolver_associados=True)).
    Recebendo qualquer um dos dois, esta função NÃO consulta de novo; sem eles
    (CLI avulsa), consulta como sempre.

    forcar=True manda TODOS os documentos da pasta pra IA de novo. Por
    padrão (forcar=False), documento que já aparece na aba 'Divergencias (IA)'
    NÃO é relido - só os novos gastam chamada de IA.

    ATENÇÃO ao que --forcar faz e ao que ele NÃO precisa mais fazer:

    O que fica em cache é a LEITURA da IA, não o resultado. As regras são
    reaplicadas em toda rodada, então mudar uma regra passou a valer na base
    inteira SEM --forcar e sem custo de IA (desde 06/10/2026).

    --forcar serve só pra quando você quer que a IA LEIA o documento de novo -
    e aí vale lembrar que ela não é determinística: na reserva 34428 o mesmo
    arquivo foi lido de dois jeitos em duas rodadas. Usar --forcar pra testar
    mudança de regra mistura as duas coisas e torna o teste inconclusivo.

    reler_telas=True (14/10/2026) relê com IA SÓ as Telas de Avaliação de Risco
    lidas antes de existir 'tabela_renda' no prompt - o que a conferência de
    renda por proponente precisa -, sem sortear de novo a leitura do resto."""
    caminhos_xlsx = list(pasta.glob("*.xlsx"))
    if not caminhos_xlsx:
        raise SystemExit(f"Não achei nenhum .xlsx dentro de {pasta}")
    caminho_xlsx = caminhos_xlsx[0]

    print(f"=== Analisando documentos de: {pasta.name} ===")
    cadastro = ler_dados_cadastrais(caminho_xlsx)
    documentos = ler_lista_documentos(caminho_xlsx)
    docs_com_arquivo = [d for d in documentos if d.get("arquivo_local")]

    # Modalidade da venda (Financiamento Bancário / Venda Direta /
    # Loteamento), via GET /v1/cvdw/reservas cruzando por idprecadastro -
    # ver classificar_modalidade() e reuniao-mila-regras-modalidade.md.
    # Pasta sem número reconhecível no nome, ou pré-cadastro que ainda não
    # virou reserva: modalidade fica None, e só as regras COMUNS a todas
    # as modalidades são aplicadas (nunca chuta uma modalidade específica).
    cfg = carregar_config()
    idprecadastro = extrair_idprecadastro_da_pasta(pasta.name)
    modalidade = None
    tipovenda_texto = None
    registro_reserva = None
    associados: list[dict] = []
    associados_da_operacao: list[dict] = []
    renda_titular = None
    if idprecadastro is not None:
        eh_reserva = numero_da_pasta_e_idreserva(pasta.name)
        registro_reserva = buscar_registro_da_reserva(
            cfg, idprecadastro, eh_idreserva=eh_reserva
        )
        tipovenda_texto = registro_reserva.get("tipovenda") if registro_reserva else None
        modalidade = classificar_modalidade(tipovenda_texto)

        # Renda do titular = renda_cliente_principal do PRÉ-CADASTRO (a renda é
        # conferida por proponente desde 14/10/2026 - ver
        # conferir_renda_da_pessoa()). Numa pasta de reserva o número do nome é
        # o idreserva, então o idprecadastro só pode vir do registro da reserva.
        #
        # BUG CORRIGIDO EM 07/10/2026, reserva 34239. Quando a busca
        # da reserva falhava - ali foi um erro 429 do CV -, o código caía em
        # `id_pre = idprecadastro`, que numa pasta de reserva é o **idRESERVA**.
        # Resultado: buscava o pré-cadastro 34239 como se fosse de pré-cadastro,
        # achava o de OUTRA pessoa, e imprimia a renda dela como se fosse da
        # cliente da reserva. Dado errado, em silêncio, com cara de certo.
        #
        # É exatamente o perigo que numero_da_pasta_e_idreserva() já documenta
        # para o caminho inverso. Agora, em pasta de reserva sem registro
        # encontrado, a renda simplesmente não é buscada.
        id_pre = None
        if eh_reserva:
            if registro_reserva:
                id_pre = registro_reserva.get("idprecadastro")
            if not id_pre:
                print(
                    "  Não consegui identificar o pré-cadastro desta reserva "
                    "(registro não encontrado na API) - a renda NÃO será comparada. "
                    "Usar o número da pasta aqui traria a renda de outra pessoa."
                )
        else:
            id_pre = idprecadastro

        if id_pre:
            pre = buscar_precadastro(cfg, id_pre)
            if pre:
                renda_titular = pre.get("renda_cliente_principal")
                print(
                    f"  Renda (pré-cadastro {id_pre}): titular R$ {renda_titular}  |  "
                    f"familiar bruta R$ {pre.get('renda_total')}"
                )
            else:
                print(f"  Não consegui ler o pré-cadastro {id_pre} - renda não será comparada")

        # Quem mais participa da operação. Serve pra NÃO acusar de divergência
        # um documento que é do 2º comprador ou do avalista - antes disso o
        # sistema comparava o documento de uma pessoa com o cadastro de outra
        # (reservas 33974 e 33905, 22/09/2026). Só existe pra reserva; em
        # pré-cadastro fica vazio e o comportamento é o de sempre.
        #
        # 09/10/2026, verificação offline do projeto: esta lista decide se o
        # documento de um co-participante vira AVISO ou DIVERGÊNCIA de nome/CPF
        # e se a renda é comparada. Ela era consultada de novo em CADA pasta (N
        # chamadas a mais na listagem que mais bate em 429), e quando a consulta
        # falhava o aviso ficava só no terminal - a planilha saía com
        # divergências falsas e sem nenhum sinal disso. Agora quem já consultou
        # (auditar_reserva, testar_amostra) repassa a lista e a falha, e a falha
        # vira linha na planilha (ver gravar_aba_divergencias). As regras de
        # nome, CPF e renda não mudam: a falha só fica visível.
        if eh_reserva:
            if associados_consultados is not None or falha_associados:
                associados = list(associados_consultados or [])
            else:
                try:
                    associados = buscar_associados_da_reserva(cfg, idprecadastro)
                except SystemExit as e:
                    # Só o começo da mensagem: o resto é o corpo cru da resposta
                    # do CV (mesmo corte de processar_reserva_completa).
                    falha_associados = str(e).split(":", 1)[0][:150]
            # A impressão de quem são as "outras pessoas" fica para DEPOIS
            # do ajuste por papel, logo abaixo. Imprimir aqui fazia a pasta
            # do 2º comprador listar ele mesmo como "outra pessoa da
            # operação" (visto na reserva 34428, 06/10/2026).
            if falha_associados:
                print(f"  FALHA ao consultar os associados ({falha_associados}) - seguindo "
                      f"só com o titular; a planilha registra a falha")
            associados_da_operacao = list(associados)

    # De QUEM é esta pasta. Pastas geradas antes de 05/10/2026, e todo
    # pré-cadastro, não têm o campo - e são do titular, que é o default.
    papel = cadastro.get("papel_na_operacao")
    if papel_e_titular(papel) and associados:
        nomes = ", ".join(
            f"{a.get('nome')} ({a.get('tipo_associacao')})" for a in associados
        )
        print(f"  Outras pessoas na operação: {nomes}")
    if not papel_e_titular(papel):
        print(f"  *** Esta pasta é do {papel}, não do titular ***")
        # 'associados' tem que significar "as OUTRAS pessoas da operação" do
        # ponto de vista de quem é o dono da pasta. Vindo do CV, a lista traz
        # os associados e não traz o titular - então, numa pasta de 2º
        # comprador, é preciso tirar a própria pessoa e colocar o titular.
        #
        # Sem isso a regra fica assimétrica num caso que é comum: a certidão
        # de casamento nomeia os DOIS cônjuges. Na pasta do titular o nome do
        # outro é reconhecido como associado e vira aviso; na pasta do 2º, o
        # titular não estaria na lista e o mesmo documento viraria DIVERGÊNCIA.
        # O mesmo papel acusado de um lado e perdoado do outro.
        doc_proprio = re.sub(r"\D", "", str(cadastro.get("documento") or ""))
        if doc_proprio:
            associados = [
                a for a in associados
                if re.sub(r"\D", "", str(a.get("documento") or "")) != doc_proprio
            ]
        if cadastro.get("titular_nome"):
            associados = associados + [{
                "nome": cadastro.get("titular_nome"),
                "documento": cadastro.get("titular_documento") or "",
                "tipo_associacao": "Titular",
            }]
        if associados:
            nomes = ", ".join(
                f"{a.get('nome')} ({a.get('tipo_associacao')})" for a in associados
            )
            print(f"  Outras pessoas da operação, vistas desta pasta: {nomes}")

    if tipovenda_texto:
        print(f"  Modalidade da venda: {tipovenda_texto} ({modalidade or 'não reconhecida - aplicando só regras comuns'})")
    else:
        print("  Modalidade da venda: desconhecida (pré-cadastro ainda não virou reserva, ou não encontrado) - aplicando só regras comuns a todas as modalidades")

    # A renda DESTA pessoa no CV e as outras pastas da mesma operação (ver
    # concluir_pasta()). Sem a lista de associados (falhou, ou é pré-cadastro)
    # não se sabe quem participa da operação: nenhuma irmã é usada, e a renda
    # só é conferida como numa operação de uma pessoa se ela de fato for.
    doc_proprio = re.sub(r"\D", "", str(cadastro.get("documento") or ""))
    if papel_e_titular(papel):
        renda_cv = renda_titular
    else:
        registro_proprio = next(
            (a for a in associados_da_operacao
             if re.sub(r"\D", "", str(a.get("documento") or "")) == doc_proprio), None)
        renda_cv = registro_proprio.get("renda_familiar") if registro_proprio else None
    documentos_da_operacao = None
    if associados_da_operacao and not falha_associados:
        documentos_da_operacao = {
            re.sub(r"\D", "", str(x or ""))
            for x in [cadastro.get("titular_documento") or doc_proprio]
            + [a.get("documento") for a in associados_da_operacao]
        }
    irmas = ler_pastas_irmas(pasta, documentos_da_operacao)
    operacao_de_uma_pessoa = not associados_da_operacao and not falha_associados
    if irmas:
        print(f"  Outras pastas da operação usadas na conferência: {len(irmas)}")

    # Documento obrigatório que nem foi anexado na pasta - checagem por
    # texto, não gasta chamada de IA. Só para o terminal: a conclusão que vai
    # para a planilha é a de concluir_pasta(), depois da leitura.
    tipos_presentes = [d.get("tipo", "") for d in documentos]
    divergencias_ausencia = checar_documentos_ausentes(
        modalidade,
        tipos_presentes + [t for i in irmas for t in i["tipos_presentes"]
                           if documento_e_tela_de_risco(t)],
        papel)
    if divergencias_ausencia:
        print(f"  {len(divergencias_ausencia)} documento(s) obrigatório(s) ausente(s) da pasta:")
        for msg in divergencias_ausencia:
            print(f"    - {msg}")

    anteriores = {} if forcar else ler_resultados_anteriores(caminho_xlsx)

    antigas: list[str] = []
    relidas_antes: dict[str, dict] = {}  # a leitura velha da Tela, se a releitura falhar
    if reler_telas:
        antigas = [a for a, r in anteriores.items()
                   if documento_e_tela_de_risco(r.get("tipo_cv") or "")
                   and "tabela_renda" not in (r.get("doc_ia") or {})
                   and str((r.get("doc_ia") or {}).get("tipo_identificado") or "") not in LEITURAS_SEM_IA]
        for arquivo in antigas:
            relidas_antes[arquivo] = anteriores.pop(arquivo)
        print(f"  --reler-telas: {len(antigas)} Tela(s) lida(s) sem a tabela RENDA vão para a IA de novo")

    # As REGRAS são reaplicadas sobre a leitura guardada, sem IA nenhuma.
    # É isto que faz regra nova valer na base já processada de graça - ver o
    # comentário em aplicar_regras().
    if anteriores:
        reaplicados = reaplicar_regras_no_cache(anteriores, cadastro, modalidade, associados)
        if reaplicados["mudaram"]:
            print(f"  {reaplicados['mudaram']} documento(s) tiveram o resultado "
                  f"recalculado pelas regras atuais (sem reler com IA).")
        if reaplicados["incompletos"]:
            print(f"  {reaplicados['incompletos']} documento(s) vêm de planilha "
                  f"antiga, sem a leitura gravada - o resultado deles é o da rodada "
                  f"anterior. Use --forcar uma vez pra torná-los recalculáveis.")

    docs_pendentes = [d for d in docs_com_arquivo if d["arquivo_local"] not in anteriores]

    if not docs_pendentes:
        print(
            f"  Todos os {len(docs_com_arquivo)} documento(s) já foram analisados numa rodada anterior. "
            f"Nada novo pra ler (use --forcar pra reprocessar tudo de novo)."
        )
        resultados = list(anteriores.values())
        _gravar_conclusoes(pasta, caminho_xlsx, cadastro, documentos, resultados, modalidade,
                           irmas, renda_cv, operacao_de_uma_pessoa, falha_associados,
                           registro_reserva)
        return

    if anteriores:
        print(
            f"  {len(anteriores)} documento(s) já lido(s) antes - pulando esses. "
            f"Lendo só {len(docs_pendentes)} documento(s) novo(s)."
        )

    # Cache persistente pelo conteúdo do arquivo (ver PASTA_CACHE_IA). Só não
    # vale para a releitura explícita: --forcar (tudo) e --reler-telas (as Telas).
    reler = {d["arquivo_local"] for d in docs_pendentes} if forcar else set(antigas)
    lock_print = threading.Lock()
    novos_resultados: list[dict] = []
    falhas_ia: list[dict] = []  # aguardando leitura (ver _resultado_de_falha)
    para_ia = []
    sem_leitura = 0
    for doc in docs_pendentes:
        tipo = doc.get("tipo", "")
        nao_le = bool(fora_do_escopo_de_validacao(tipo) or dispensado_pela_modalidade(tipo, modalidade))
        leitura = None if (nao_le or doc["arquivo_local"] in reler) else leitura_do_cache(pasta / doc["arquivo_local"])
        if leitura is None and not nao_le:
            para_ia.append(doc)
            continue
        sem_leitura += nao_le  # CTPS, Simulação CCA...: nem cache nem IA
        resultado = _processar_um_documento(
            None, "", pasta, doc, cadastro, len(novos_resultados) + 1, len(docs_pendentes), lock_print,
            modalidade, associados, leitura_pronta=leitura)
        if resultado is not None:
            (falhas_ia if resultado.get("falha_ia") else novos_resultados).append(resultado)
    print(f"  cache da IA: {len(docs_pendentes) - len(para_ia) - sem_leitura} do cache, "
          f"{len(para_ia)} para a IA, {sem_leitura} fora do escopo")
    if not para_ia:
        _concluir_e_resumir(pasta, caminho_xlsx, cadastro, documentos, list(anteriores.values()) + novos_resultados,
                            modalidade, irmas, renda_cv, operacao_de_uma_pessoa, falha_associados,
                            registro_reserva, falhas_ia)
        return

    chave_ia = (
        cfg.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not chave_ia:
        raise SystemExit(
            "Faltou a chave da IA. Adicione 'gemini_api_key' no config.json "
            "ou defina a variável de ambiente GEMINI_API_KEY."
        )
    modelo = cfg.get("modelo_ia", MODELO_IA_PADRAO)
    paralelismo = int(cfg.get("paralelismo_ia", PARALELISMO_PADRAO))
    client = criar_cliente_ia(chave_ia)
    lidos = _ler_com_a_ia(client, modelo, paralelismo, pasta, para_ia, cadastro, lock_print,
                          modalidade, associados)
    # Falha de sistema fica FORA dos resultados (nem divergência, nem linha da aba
    # de divergências, então a próxima rodada a lê de novo) e vai para a aba
    # própria. Na releitura das Telas, a leitura velha continua valendo.
    falhas_ia += [r for r in lidos if r.get("falha_ia")]
    novos_resultados += [r for r in lidos if not r.get("falha_ia")]
    novos_resultados += [relidas_antes[r["arquivo"]] for r in falhas_ia if r["arquivo"] in relidas_antes]

    _concluir_e_resumir(pasta, caminho_xlsx, cadastro, documentos, list(anteriores.values()) + novos_resultados,
                        modalidade, irmas, renda_cv, operacao_de_uma_pessoa, falha_associados,
                        registro_reserva, falhas_ia)


def _concluir_e_resumir(pasta, caminho_xlsx, cadastro, documentos, resultados, modalidade, irmas,
                        renda_cv, operacao_de_uma_pessoa, falha_associados, registro_reserva,
                        falhas_ia: list[dict] | None = None) -> None:
    conclusoes = _gravar_conclusoes(
        pasta, caminho_xlsx, cadastro, documentos, resultados, modalidade, irmas, renda_cv,
        operacao_de_uma_pessoa, falha_associados, registro_reserva, falhas_ia)
    divergencias_ausencia = conclusoes["divergencias_ausencia"]
    total_divergencias = (sum(1 for r in resultados if r["divergencias"])
                          + (1 if divergencias_ausencia else 0)
                          + len(conclusoes["divergencias_pasta"])
                          + sum(1 for _, nivel, _ in conclusoes["extras"] if nivel == "divergencia"))
    total_avisos = sum(1 for r in resultados if not r["divergencias"] and r["avisos"])
    print(
        f"\nConcluído. {total_divergencias}/{len(resultados)} documento(s)/item(ns) com divergência "
        f"(incluindo {len(divergencias_ausencia)} documento(s) ausente(s)), "
        f"{total_avisos}/{len(resultados)} só com aviso (revisar, mas não é erro)."
    )
    if falhas_ia:
        print(f"{len(falhas_ia)} documento(s) AGUARDANDO LEITURA por falha da IA (aba '{ABA_FALHAS_IA}'): "
              f"não foram conferidos e serão lidos de novo na próxima rodada.")
    print(f"Veja a aba 'Divergencias (IA)' em: {caminho_xlsx}")


def _gravar_conclusoes(
    pasta: Path, caminho_xlsx: Path, cadastro: dict, documentos: list[dict],
    resultados: list[dict], modalidade: str | None, irmas: list[dict], renda_cv,
    operacao_de_uma_pessoa: bool, falha_associados: str | None, registro_reserva,
    falhas_ia: list[dict] | None = None,
) -> dict:
    """Fecha a pasta: concluir_pasta(), terminal e as abas (divergências,
    contexto e falhas da IA). 'resultados' não traz os documentos com falha."""
    conclusoes = concluir_pasta(pasta, cadastro, documentos, resultados, modalidade,
                                irmas, renda_cv, operacao_de_uma_pessoa)
    if conclusoes["divergencias_pasta"]:
        print(f"  *** ATENÇÃO: {len(conclusoes['divergencias_pasta'])} caso(s) de arquivo "
              f"repetido em campos diferentes ***")
        for msg in conclusoes["divergencias_pasta"]:
            print(f"    - {msg}")
    for msg in conclusoes["avisos_campo_errado"]:
        print(f"  (aviso) {msg}")
    for rotulo, nivel, msg in conclusoes["extras"]:
        print(f"  ({rotulo}, {nivel}) {msg}")
    gravar_aba_divergencias(caminho_xlsx, resultados, conclusoes["divergencias_ausencia"],
                            conclusoes["divergencias_pasta"], conclusoes["avisos_campo_errado"],
                            falha_associados, conclusoes["extras"])
    # Mesmo registro já consultado acima - nenhuma chamada nova ao CV.
    gravar_aba_contexto(caminho_xlsx, contexto_da_reserva(registro_reserva, modalidade))
    gravar_aba_falhas_ia(caminho_xlsx, falhas_ia or [])
    return conclusoes


def verificar_chave_ia() -> None:
    """Checagem rápida, ANTES de processar qualquer pasta - evita rodar
    --todas e falhar pasta por pasta repetindo o mesmo erro de config."""
    import os

    cfg = carregar_config()
    chave_ia = (
        cfg.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not chave_ia:
        raise SystemExit(
            "Faltou a chave da IA. Adicione 'gemini_api_key' no config.json "
            "ou defina a variável de ambiente GEMINI_API_KEY."
        )


def processar_todas_as_pastas(forcar: bool = False) -> None:
    """Roda processar_pasta_cliente() em cada pasta de saidas/, uma por
    uma. Se uma pasta falhar (sem .xlsx, Excel corrompido etc.) não trava
    as outras - registra e segue pra próxima, com um resumo no final."""
    verificar_chave_ia()
    pastas = listar_pastas_de_reserva()
    if not pastas:
        print(
            f"Nenhuma pasta encontrada em {PASTA_SAIDAS_RESERVA}. "
            "Rode o mvp_v2_reserva.py primeiro."
        )
        return

    falhas: list[tuple[str, str]] = []
    for pasta in pastas:
        try:
            processar_pasta_cliente(pasta, forcar=forcar)
        except SystemExit as exc:
            # Uma pasta com problema (sem .xlsx, Excel corrompido, sem
            # chave de IA etc.) não pode travar as outras - registra e
            # segue pra próxima.
            print(f"  PULADA - {pasta.name}: {exc}")
            falhas.append((pasta.name, str(exc)))
        print()

    if falhas:
        print(f"=== {len(falhas)} pasta(s) pulada(s) por erro ===")
        for nome, motivo in falhas:
            print(f"  - {nome}: {motivo}")


def encontrar_pasta_por_idprecadastro(idprecadastro: int) -> Path | None:
    """As pastas são nomeadas '{nome do cliente}-{idprecadastro}' (pré-cadastro,
    mvp_v1_validador.py) ou '{nome do cliente}-reserva-{idreserva}' (reserva,
    mvp_v2_reserva.py). Procura em ambas as subpastas a que termina com esse
    número - funciona tanto pra idprecadastro quanto pra idreserva."""
    sufixo = f"-{idprecadastro}"
    for pasta in listar_pastas_de_cliente():
        if pasta.name.endswith(sufixo):
            return pasta
    return None


def pedir_idprecadastro() -> int:
    """Pergunta o número do pré-cadastro OU da reserva no terminal,
    repetindo até vir um número válido (mesmo padrão do mvp_v1_validador.py)."""
    while True:
        entrada = input("Número do pré-cadastro ou da reserva: ").strip()
        if entrada.isdigit():
            return int(entrada)
        print("Digite apenas números (ex: 34139). Tente de novo.")


def pedir_forcar() -> bool:
    resposta = input("Reprocessar tudo de novo, ignorando o que já foi lido antes? (s/N): ").strip().lower()
    return resposta == "s"


def menu_interativo() -> None:
    print("O que você quer analisar?")
    print("  1 - Uma pasta específica (informar o número do pré-cadastro ou da reserva)")
    print("  2 - TODAS as pastas de reserva (saidas/reserva/)")
    escolha = input("Escolha 1 ou 2: ").strip()
    forcar = pedir_forcar()

    if escolha == "2":
        processar_todas_as_pastas(forcar=forcar)
        return

    idprecadastro = pedir_idprecadastro()
    pasta = encontrar_pasta_por_idprecadastro(idprecadastro)
    if pasta is None:
        print(f"Não encontrei nenhuma pasta terminando em '-{idprecadastro}' em pre_cadastro/ ou reserva/.")
        print("Confira se já rodou o mvp_v1_validador.py (pré-cadastro) ou o mvp_v2_reserva.py (reserva) pra esse número.")
        return

    verificar_chave_ia()
    processar_pasta_cliente(pasta, forcar=forcar)


def reler_telas_das_pastas(pastas: list[Path]) -> list[dict]:
    global ORIGEM_DO_CONSUMO
    ORIGEM_DO_CONSUMO = "releitura"
    """--reler-telas, reserva por reserva. Duas passadas por reserva: a primeira
    relê as Telas antigas; a segunda, sem IA, deixa cada pasta ver a Tela relida
    nas irmãs. Titular antes dos demais.

    Uma reserva que quebra (erro do CV, da IA, planilha aberta) é registrada e
    a próxima segue - o mesmo que varredura_diaria.py faz (revisão da Forja,
    15/10/2026). Devolve as falhas: [{"reserva", "erro"}]."""
    grupos: dict[str, list[Path]] = {}
    for pasta in pastas:
        m = re.search(r"-reserva-(\d+)$", pasta.name)
        grupos.setdefault(m.group(0) if m else pasta.name, []).append(pasta)

    def dono_primeiro(pasta: Path) -> bool:
        try:
            xlsx = next(pasta.glob("*.xlsx"))
            return not papel_e_titular(ler_dados_cadastrais(xlsx).get("papel_na_operacao"))
        except Exception:
            return True

    # A MESMA trava da varredura e do executor, com modo "releitura", reserva
    # por reserva (19/10/2026: rodado à mão, gravava as mesmas planilhas que eles).
    import varredura_diaria as vd

    falhas: list[dict] = []
    for chave, do_grupo in grupos.items():
        do_grupo.sort(key=dono_primeiro)
        token, _ = vd._pegar_trava_esperando("releitura")
        if token is None:
            print(f"  PULADA {chave}: a trava está com a varredura")
            falhas.append({"reserva": chave, "erro": "trava ocupada pela varredura"})
            continue
        try:
            for releitura in (True, False):
                for pasta in do_grupo:
                    processar_pasta_cliente(pasta, reler_telas=releitura)
                    print()
        except (Exception, SystemExit) as e:  # uma reserva quebrada não derruba as outras
            print(f"  FALHOU {chave}: {str(e)[:200]}")
            falhas.append({"reserva": chave, "erro": str(e).split(":", 1)[0][:120]})
        finally:
            vd._soltar_trava(token)
    print(f"\n--reler-telas: {len(grupos) - len(falhas)}/{len(grupos)} reserva(s)/pasta(s) ok, "
          f"{len(falhas)} falha(s)")
    for f in falhas:
        print(f"  - {f['reserva']}: {f['erro']}")
    return falhas


def main() -> None:
    argv = sys.argv[1:]
    forcar = "--forcar" in argv
    reler_telas = "--reler-telas" in argv
    argv = [a for a in argv if a not in ("--forcar", "--reler-telas")]

    if argv == ["--alimentar-cache"]:
        print(alimentar_cache_com_as_planilhas())
        return
    if argv == ["--revisar-cache"]:
        print(revisar_cache())
        return

    if reler_telas:
        # Uma reserva (todas as pastas dela) ou --todas. Duas passadas: a
        # primeira relê as Telas antigas; a segunda, sem IA, deixa cada pasta
        # ver a Tela relida nas irmãs (a do titular roda antes da do 2º).
        if argv == ["--todas"]:
            verificar_chave_ia()
            pastas = listar_pastas_de_reserva()
        elif len(argv) == 1 and argv[0].isdigit():
            verificar_chave_ia()
            # Só '-reserva-{n}': a pasta de pré-cadastro com o mesmo número é de
            # outra pessoa (armadilha 5).
            pastas = [p for p in listar_pastas_de_reserva()
                      if p.name.endswith(f"-reserva-{argv[0]}")]
        else:
            print("Uso: python analisar_documentos.py (33773 | --todas) --reler-telas")
            sys.exit(1)
        reler_telas_das_pastas(pastas)
        return

    if len(argv) == 0:
        menu_interativo()
        return

    if len(argv) == 1 and argv[0] == "--todas":
        processar_todas_as_pastas(forcar=forcar)
        return

    if len(argv) == 1:
        verificar_chave_ia()
        # Só número: trata como idprecadastro/idreserva e procura a pasta pelo
        # sufixo, igual ao menu interativo faz. Antes de 09/09/2026 a linha de
        # comando só aceitava o nome exato da pasta, então
        # "analisar_documentos.py 33773" respondia "Pasta não encontrada" mesmo
        # com a pasta lá - o menu interativo funcionava e a linha de comando não.
        if argv[0].isdigit():
            pasta = encontrar_pasta_por_idprecadastro(int(argv[0]))
            if pasta is None:
                print(f"Não encontrei nenhuma pasta terminando em '-{argv[0]}' em pre_cadastro/ ou reserva/.")
                sys.exit(1)
        else:
            pasta = encontrar_pasta_de_cliente(argv[0])
            if pasta is None:
                print(f"Pasta não encontrada: '{argv[0]}' (procurei em pre_cadastro/, reserva/ e na raiz de saidas/)")
                sys.exit(1)
        processar_pasta_cliente(pasta, forcar=forcar)
        return

    print("Uso:")
    print("  python analisar_documentos.py                                  (pergunta no terminal)")
    print("  python analisar_documentos.py 33773 [--forcar]                 (número do pré-cadastro ou da reserva)")
    print('  python analisar_documentos.py "NOME DO CLIENTE-idprecadastro" [--forcar]')
    print("  python analisar_documentos.py --todas [--forcar]")
    print("  python analisar_documentos.py (33773 | --todas) --reler-telas  (relê só as Telas sem a tabela RENDA)")
    print()
    print("Por padrão, documento que já foi analisado numa rodada anterior NÃO é lido de")
    print("novo (economiza chamada de IA). Use --forcar pra reprocessar tudo do zero.")
    sys.exit(1)


if __name__ == "__main__":
    main()
