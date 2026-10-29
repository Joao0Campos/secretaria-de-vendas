"""
Auditoria completa de uma RESERVA - ponto único de entrada
===========================================================

Junta num comando só o que antes exigia rodar dois scripts em sequência, e
acrescenta uma etapa nova de SUGESTÃO de correção.

Você informa o número da reserva e ele:

    1) Puxa a documentação anexada no CV e baixa os arquivos
    2) Puxa os dados cadastrais do cliente no CV
    3) Monta a planilha e aponta as divergências (leitura por IA)
    4) Lista as OUTRAS pessoas da operação (2º comprador, avalista, cônjuge)
    5) SUGERE quais campos poderiam ser corrigidos via API

⚠️⚠️⚠️  ESTE SCRIPT NÃO ALTERA NADA NO CV  ⚠️⚠️⚠️
=====================================================

A etapa 4 é APENAS SUGESTÃO. Este script:

  - NÃO chama nenhum endpoint de escrita (PATCH/PUT/POST/DELETE)
  - NÃO importa nenhuma função que escreva no CV
  - NÃO pede senha (não gera Bearer Token, que é o que a escrita exigiria)

Tudo que ele faz contra o CV é LEITURA (GET). A sugestão sai numa aba
própria da planilha e no terminal, pra uma PESSOA decidir e executar
manualmente, no próprio CV (o script de escrita usado internamente não faz
parte desta cópia pública).

Essa separação é proposital. A validação contratual existe
justamente para conferir o que foi preenchido no CV;
uma automação que corrige cadastro sozinha, sem ninguém olhando, seria
resolver um problema de confiança criando outro maior.

Como rodar:

    python auditar_reserva.py                 -> pergunta no terminal
    python auditar_reserva.py 33773           -> audita essa reserva
    python auditar_reserva.py 33773 --forcar  -> relê todos os documentos com IA

Sem --forcar, documentos já lidos numa rodada anterior são reaproveitados
(economiza chamada de IA). Use --forcar quando quiser reprocessar do zero.

Configuração: o mesmo config.json dos outros scripts (dominio, email,
token, gemini_api_key).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# IMPORTANTE: só entram aqui funções de LEITURA.
#
# Repare que NADA do script de escrita no CV (testar_alteracao_pessoa.py, que
# não faz parte desta cópia pública) é importado. Manter esse arquivo
# fora do import é o que garante, no nível do código e não só da intenção,
# que rodar este script não altera cadastro.
# ---------------------------------------------------------------------------
from mvp_v2_reserva import processar_reserva_completa
from analisar_documentos import (
    processar_pasta_cliente,
    ler_dados_cadastrais,
    parse_valor_monetario,
    formatar_reais,
)


# ---------------------------------------------------------------------------
# Etapa 4: sugestão de correção
# ---------------------------------------------------------------------------
#
# Cada divergência que o analisador gera é uma frase de texto. Aqui elas são
# reconhecidas por padrão e traduzidas em "campo do CV + valor sugerido".
#
# O nome do campo é o aceito pelo PATCH /v3/cadastros/pessoas/{id} (a lista
# completa está na documentação da API v3 do CV CRM).
#
# Divergência que não casa com nenhum padrão daqui NÃO é descartada: ela sai
# como "requer análise manual". Preferir mostrar demais a esconder de menos -
# uma sugestão perdida é uma correção que ninguém vai fazer.

PADROES_DE_SUGESTAO = [
    {
        "campo": "rendaFamiliar",
        "rotulo": "Renda familiar bruta",
        # Renda do documento (R$ 6.731,69) difere da renda cadastrada (R$ 4.010,57) - ...
        "regex": re.compile(
            r"Renda do documento \(R\$ ([\d.,]+)\) difere da renda cadastrada \(R\$ ([\d.,]+)\)"
        ),
        "monetario": True,
    },
    {
        "campo": "documento",
        "rotulo": "CPF / CNPJ",
        # CPF no documento (00000000000) difere do cadastro (11111111111)
        "regex": re.compile(
            r"CPF no documento \((\d+)\) difere do cadastro \((\d+)\)"
        ),
        "monetario": False,
        # Alterar CPF é a operação mais perigosa desta lista. Em 15/09/2026, na
        # reserva 33892, um CPF gravado com pontuação fez o CV
        # deixar de reconhecer a pessoa e criar cadastro DUPLICADO - o contrato
        # saiu com CPF errado e o suporte do CV CRM teve que intervir
        # manualmente. Antes de aplicar, confira se já não
        # existe outro cadastro com esse mesmo CPF.
        "atencao": (
            "RISCO ALTO: trocar CPF pode gerar cadastro duplicado no CV. "
            "Antes de aplicar, procure em Pessoas se já existe cadastro com esse CPF."
        ),
    },
    {
        "campo": "nome",
        "rotulo": "Nome",
        # Nome no documento ('X') difere do cadastro ('Y') - similaridade N%
        "regex": re.compile(
            r"Nome no documento \('(.+?)'\) difere do cadastro \('(.+?)'\)"
        ),
        "monetario": False,
    },
    {
        "campo": "endereco",
        "rotulo": "Endereço",
        # Endereço no comprovante ('X') não bate com o endereço cadastrado ('Y') - ...
        "regex": re.compile(
            r"Endereço no comprovante \('(.+?)'\) não bate com o endereço cadastrado \('(.+?)'\)"
        ),
        "monetario": False,
    },
]

# Divergências que NUNCA viram sugestão de alteração de cadastro, porque o
# problema está no DOCUMENTO anexado, não no que está cadastrado. Corrigir o
# cadastro nesses casos seria mascarar o problema em vez de resolvê-lo.
PADROES_NAO_SAO_ERRO_DE_CADASTRO = [
    "Documento obrigatório não encontrado",
    "Não foi possível ler o documento",
    "Documento ilegível",
    "Comprovante de endereço com",
    "Validade vencida",
    "Não foi possível identificar",
    "Data de emissão",
]


def eh_problema_do_documento(divergencia: str) -> bool:
    return any(p.lower() in divergencia.lower() for p in PADROES_NAO_SAO_ERRO_DE_CADASTRO)


def montar_sugestoes(caminho_xlsx: Path) -> list[dict]:
    """Lê a aba 'Divergencias (IA)' já gravada e traduz cada divergência numa
    sugestão de correção. NÃO executa nada - só monta a lista."""
    from openpyxl import load_workbook

    wb = load_workbook(caminho_xlsx, data_only=True)
    if "Divergencias (IA)" not in wb.sheetnames:
        return []

    ws = wb["Divergencias (IA)"]
    cabecalho = [c.value for c in ws[1]]
    sugestoes: list[dict] = []

    for linha in ws.iter_rows(min_row=2, values_only=True):
        if not any(linha):
            continue
        d = dict(zip(cabecalho, linha))
        arquivo = d.get("arquivo") or ""
        texto_divergencias = str(d.get("divergencias") or "")
        if not texto_divergencias or texto_divergencias == "OK":
            continue

        for divergencia in texto_divergencias.split(" | "):
            divergencia = divergencia.strip()
            if not divergencia:
                continue

            if eh_problema_do_documento(divergencia):
                sugestoes.append({
                    "campo": "-",
                    "rotulo": "Problema no documento anexado",
                    "valor_atual": "-",
                    "valor_sugerido": "-",
                    "acao": "Não é erro de cadastro: pedir documento correto ao correspondente",
                    "origem": arquivo,
                    "divergencia": divergencia,
                    "atencao": "",
                })
                continue

            casou = False
            for padrao in PADROES_DE_SUGESTAO:
                m = padrao["regex"].search(divergencia)
                if not m:
                    continue
                valor_documento, valor_cadastro = m.group(1), m.group(2)

                if padrao["monetario"]:
                    numero = parse_valor_monetario(valor_documento)
                    valor_para_api = f"{numero:.2f}" if numero is not None else valor_documento
                    exibir_doc = f"R$ {valor_documento}"
                    exibir_cad = f"R$ {valor_cadastro}"
                else:
                    valor_para_api = valor_documento
                    exibir_doc = valor_documento
                    exibir_cad = valor_cadastro

                sugestoes.append({
                    "campo": padrao["campo"],
                    "rotulo": padrao["rotulo"],
                    "valor_atual": exibir_cad,
                    "valor_sugerido": exibir_doc,
                    "acao": f"Atualizar '{padrao['campo']}' para: {valor_para_api}",
                    "origem": arquivo,
                    "divergencia": divergencia,
                    "atencao": padrao.get("atencao", ""),
                })
                casou = True
                break

            if not casou:
                sugestoes.append({
                    "campo": "?",
                    "rotulo": "Requer análise manual",
                    "valor_atual": "-",
                    "valor_sugerido": "-",
                    "acao": "Divergência sem correção automática mapeada - avaliar caso a caso",
                    "origem": arquivo,
                    "divergencia": divergencia,
                    "atencao": "",
                })

    return sugestoes


def gravar_aba_sugestoes(caminho_xlsx: Path, sugestoes: list[dict], idpessoa) -> None:
    """Grava a aba 'Sugestoes (NAO aplicadas)'. O nome da aba é proposital -
    quem abrir a planilha tem que entender de cara que nada foi executado."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = load_workbook(caminho_xlsx)
    if "Sugestoes (NAO aplicadas)" in wb.sheetnames:
        del wb["Sugestoes (NAO aplicadas)"]
    ws = wb.create_sheet("Sugestoes (NAO aplicadas)")

    ws.append(["*** NADA FOI ALTERADO NO CV. Esta aba é só sugestão para conferência humana. ***"])
    ws["A1"].font = Font(bold=True, color="9B3B32", size=12)
    ws.append([f"Pessoa no CV (idpessoa): {idpessoa or 'não identificado'}"])
    ws.append([])

    colunas = ["campo_no_cv", "o_que_e", "valor_atual_no_cadastro",
               "valor_sugerido", "acao_sugerida", "atencao", "documento_de_origem",
               "divergencia_detectada"]
    ws.append(colunas)
    linha_cabecalho = ws.max_row
    for cel in ws[linha_cabecalho]:
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = PatternFill("solid", fgColor="0E6070")

    for s in sugestoes:
        ws.append([
            s["campo"], s["rotulo"], s["valor_atual"], s["valor_sugerido"],
            s["acao"], s.get("atencao", ""), s["origem"], s["divergencia"],
        ])
        if s.get("atencao"):
            cor = "F7E3E5"          # vermelho: alteração de risco
        elif s["campo"] in ("-", "?"):
            cor = "FBF1D6"          # amarelo: não se resolve no cadastro
        else:
            cor = None
        if cor:
            for cel in ws[ws.max_row]:
                cel.fill = PatternFill("solid", fgColor=cor)

    for idx, largura in enumerate([18, 28, 30, 30, 45, 50, 34, 60], start=1):
        ws.column_dimensions[get_column_letter(idx)].width = largura
    for linha in ws.iter_rows(min_row=linha_cabecalho):
        for cel in linha:
            cel.alignment = Alignment(vertical="top", wrap_text=True)

    wb.save(caminho_xlsx)


def gravar_aba_associados(
    caminho_xlsx: Path, associados: list[dict], falha: str | None = None,
) -> None:
    """Aba com as OUTRAS pessoas da reserva. Até 22/09/2026 elas eram
    invisíveis: a validação só enxergava o titular, e um 2º comprador podia
    entrar na operação sem nenhum documento conferido.

    'falha' é o motivo curto quando a consulta de associados no CV falhou
    (ver processar_reserva_completa)."""
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = load_workbook(caminho_xlsx)
    if "Associados" in wb.sheetnames:
        del wb["Associados"]
    ws = wb.create_sheet("Associados")

    # 09/10/2026, verificação offline do projeto. Com a consulta falhando (429
    # esgotado, erro da API) a lista chegava vazia e esta aba dizia "Nenhum
    # associado ativo - só o titular": a planilha entregue afirmava o contrário
    # do que aconteceu, e quem lê não tinha como saber que o 2º comprador ficou
    # sem validação. Falha agora é falha, escrita e destacada.
    if falha:
        ws.append([
            f"FALHA ao consultar os associados no CV: {falha}. As outras pessoas "
            f"da operação NÃO foram validadas. Rode de novo."
        ])
        ws["A1"].font = Font(bold=True, color="FFFFFF", size=12)
        ws["A1"].fill = PatternFill("solid", fgColor="9B3B32")
        ws.column_dimensions["A"].width = 110
        wb.save(caminho_xlsx)
        return

    if not associados:
        ws.append(["Nenhum associado ativo nesta reserva - só o titular."])
        ws["A1"].font = Font(bold=True)
        ws.column_dimensions["A"].width = 60
        wb.save(caminho_xlsx)
        return

    # O texto fixo aqui dizia "A validação de documentos cobre SÓ O TITULAR",
    # falso desde 05/10/2026: cada pessoa tem a sua pasta e a sua planilha.
    # Corrigido em 09/10/2026 - o mesmo erro que imprimir_associados() já tinha
    # corrigido no terminal em 06/10.
    ws.append([
        f"*** {len(associados)} pessoa(s) além do titular nesta operação. "
        f"Cada pessoa com documentos tem a sua própria planilha; quem não tem "
        f"planilha própria NÃO teve os documentos conferidos (o motivo sai no "
        f"terminal). ***"
    ])
    ws["A1"].font = Font(bold=True, color="9B3B32", size=12)
    ws.append([])

    colunas = ["tipo_associacao", "nome", "documento", "renda_familiar",
               "idpessoa", "idassociado", "data_cad"]
    ws.append(colunas)
    linha_cabecalho = ws.max_row
    for cel in ws[linha_cabecalho]:
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = PatternFill("solid", fgColor="0E6070")

    for a in associados:
        ws.append([
            a.get("tipo_associacao", ""),
            a.get("nome", ""),
            a.get("documento", ""),
            a.get("renda_familiar", ""),
            a.get("idpessoa", ""),
            a.get("idassociado", ""),
            a.get("data_cad", ""),
        ])

    for idx, largura in enumerate([28, 38, 18, 18, 12, 14, 20], start=1):
        ws.column_dimensions[get_column_letter(idx)].width = largura
    for linha in ws.iter_rows(min_row=linha_cabecalho):
        for cel in linha:
            cel.alignment = Alignment(vertical="top", wrap_text=True)

    wb.save(caminho_xlsx)


def imprimir_associados(
    associados: list[dict], validados: set | None = None, falha: str | None = None,
) -> None:
    """'validados' são os nomes das pessoas cujos documentos ESTE run conferiu.

    O texto que ficava aqui até 06/10/2026 avisava que "a validação cobre SÓ o
    titular". Depois que o processar_reserva_completa() passou a validar todo
    mundo, esse aviso virou mentira - e foi exatamente o que a reserva 34428
    imprimiu, logo abaixo de ter conferido os 7 documentos do 2º comprador.
    Ferramenta que diz não ter feito o que acabou de fazer perde a confiança de
    quem lê o relatório, então agora o texto é calculado, não fixo."""
    validados = validados or set()
    print()
    print("=" * 72)
    print("OUTRAS PESSOAS DA OPERAÇÃO")
    print("=" * 72)
    if falha:
        # Lista vazia por FALHA não é "só o titular" (09/10/2026, ver
        # gravar_aba_associados).
        print(f"FALHA ao consultar os associados no CV: {falha}.")
        print("As outras pessoas da operação NÃO foram validadas. Rode de novo.")
        return
    if not associados:
        print("Nenhum associado ativo - a reserva é só do titular.")
        return

    print(f"\n{len(associados)} pessoa(s) além do titular:\n")
    nao_conferidos = []
    for a in associados:
        nome = a.get("nome", "?")
        conferido = nome in validados
        marca = "documentos conferidos" if conferido else "documentos NÃO conferidos"
        if not conferido:
            nao_conferidos.append(nome)
        print(f"  {a.get('tipo_associacao', '?')}: {nome}  [{marca}]")
        print(f"      CPF: {a.get('documento', '?')}   idpessoa: {a.get('idpessoa', '?')}")
    print()
    if nao_conferidos:
        print("  ATENÇÃO: não foi possível conferir os documentos de "
              f"{', '.join(nao_conferidos)}.")
        print("  Ou o CV não devolveu documentos dessa(s) pessoa(s), ou não deu pra")
        print("  identificar a qual delas o grupo de documentos pertence (nesse caso")
        print("  o motivo aparece lá em cima, no passo [4/5]).")
    else:
        print("  Os documentos de todas as pessoas acima foram conferidos nesta rodada.")


def imprimir_sugestoes(sugestoes: list[dict], idpessoa) -> None:
    print()
    print("=" * 72)
    print("SUGESTÕES DE CORREÇÃO - NADA FOI ALTERADO NO CV")
    print("=" * 72)

    if not sugestoes:
        print("Nenhuma divergência encontrada. Não há o que sugerir.")
        return

    corrigiveis = [s for s in sugestoes if s["campo"] not in ("-", "?")]
    outros = [s for s in sugestoes if s["campo"] in ("-", "?")]

    if corrigiveis:
        print(f"\n{len(corrigiveis)} campo(s) do cadastro que PODERIAM ser corrigidos via API:\n")
        for s in corrigiveis:
            print(f"  [{s['campo']}] {s['rotulo']}")
            print(f"      Cadastro hoje : {s['valor_atual']}")
            print(f"      Documento diz : {s['valor_sugerido']}")
            print(f"      Origem        : {s['origem']}")
            if s.get("atencao"):
                print(f"      ⚠️  {s['atencao']}")
            print()

    if outros:
        print(f"{len(outros)} item(ns) que NÃO se resolvem alterando cadastro:\n")
        for s in outros:
            print(f"  - {s['divergencia']}")
            print(f"      {s['acao']}")
            print()

    if corrigiveis:
        print("-" * 72)
        print("Para aplicar qualquer uma dessas correções, altere o cadastro no CV")
        print(f"    (idpessoa {idpessoa or '<idpessoa>'}). Este sistema só lê o CV.")
        print("-" * 72)


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------

def auditar(idreserva: int, forcar: bool = False) -> None:
    print("=" * 72)
    print(f"AUDITORIA DA RESERVA {idreserva}")
    print("Somente leitura - este script não altera nada no CV.")
    print("=" * 72)
    print()

    # Etapas 1 e 2: documentação + dados cadastrais (GET), e a planilha base.
    #
    # Desde 05/10/2026 isso devolve UMA PASTA POR PESSOA da operação, não só
    # a do titular - a varredura de 28/09 mediu que 47% das reservas têm 2º
    # proponente, então quase metade das vendas vinha sendo auditada pela
    # metade. Cada pasta tem o cadastro da SUA pessoa, e é contra ele que
    # nome, CPF e endereço dos documentos são comparados.
    #
    # Quem está na operação vem da MESMA consulta que separou as pastas, e é
    # gravado na planilha de todos. Até 09/10/2026 havia uma segunda busca
    # aqui: o dobro de chamadas na listagem que mais bate em 429, e duas
    # respostas que podiam discordar entre si dentro do mesmo relatório.
    pastas, associados, falha_associados = processar_reserva_completa(
        idreserva, devolver_associados=True
    )

    planilhas: list[Path] = []
    todas_sugestoes: list[tuple[str, list[dict], object]] = []
    validados: set[str] = set()   # quem teve os documentos realmente conferidos

    for pasta in pastas:
        # Etapa 3: leitura por IA e apontamento das divergências.
        print()
        # A MESMA lista (e a falha, se houve) vai para cada pasta - sem isso
        # cada pasta consultava o CV de novo (09/10/2026).
        processar_pasta_cliente(pasta, forcar=forcar,
                                associados_consultados=associados,
                                falha_associados=falha_associados)

        caminhos_xlsx = list(pasta.glob("*.xlsx"))
        if not caminhos_xlsx:
            print(f"      não achei a planilha dentro de {pasta} - pulando")
            continue
        caminho_xlsx = caminhos_xlsx[0]

        # Etapa 4 e 5: associados e sugestões (nunca aplicadas).
        gravar_aba_associados(caminho_xlsx, associados, falha_associados)
        cadastro = ler_dados_cadastrais(caminho_xlsx)
        idpessoa = cadastro.get("idpessoa")
        papel = cadastro.get("papel_na_operacao") or "titular"
        sugestoes = montar_sugestoes(caminho_xlsx)
        gravar_aba_sugestoes(caminho_xlsx, sugestoes, idpessoa)
        planilhas.append(caminho_xlsx)
        todas_sugestoes.append((f"{cadastro.get('nome')} ({papel})", sugestoes, idpessoa))
        validados.add(str(cadastro.get("nome") or ""))

    imprimir_associados(associados, validados, falha_associados)

    for rotulo, sugestoes, idpessoa in todas_sugestoes:
        print()
        print("-" * 72)
        print(f"  {rotulo}")
        print("-" * 72)
        imprimir_sugestoes(sugestoes, idpessoa)

    print()
    if len(planilhas) == 1:
        print(f"Planilha completa: {planilhas[0]}")
    else:
        print(f"{len(planilhas)} planilha(s), uma por pessoa da operação:")
        for c in planilhas:
            print(f"  - {c}")
    print("Abas: 'Dados Cadastrais', 'Documentos', 'Divergencias (IA)', "
          "'Associados', 'Sugestoes (NAO aplicadas)'")


def pedir_idreserva() -> int:
    while True:
        entrada = input("Número da reserva (idReserva): ").strip()
        if entrada.isdigit():
            return int(entrada)
        print("Digite apenas números (ex: 33773). Tente de novo.")


def main() -> None:
    argv = sys.argv[1:]
    forcar = "--forcar" in argv
    argv = [a for a in argv if a != "--forcar"]

    if len(argv) == 0:
        idreserva = pedir_idreserva()
        resposta = input("Reler todos os documentos com IA, ignorando o que já foi lido? (s/N): ").strip().lower()
        forcar = resposta == "s"
    elif len(argv) == 1 and argv[0].isdigit():
        idreserva = int(argv[0])
    else:
        print("Uso:")
        print("  python auditar_reserva.py                 (pergunta no terminal)")
        print("  python auditar_reserva.py 33773 [--forcar]")
        sys.exit(1)

    auditar(idreserva, forcar=forcar)


if __name__ == "__main__":
    main()
