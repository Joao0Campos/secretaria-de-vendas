"""
Consolidado - relatório único com todas as pastas de saidas/
==============================================================

Roda DEPOIS do mvp_v1_validador.py e do analisar_documentos.py. Percorre
TODAS as pastas de cliente dentro de saidas/, junta o resultado de cada
uma (o que já está na aba "Divergencias (IA)" de cada Excel) com os dados
do pré-cadastro no CV (empreendimento, unidade, situação - via
GET /v1/cvdw/precadastros, mesma autenticação de sempre, sem custo de IA)
e gera UM ÚNICO Excel, direto dentro de saidas/ (não dentro de nenhuma
pasta de cliente), com uma linha por pré-cadastro:

    Cliente | Empreendimento | Nº Pré-cadastro | Unidade | Situação |
    Pontuação (divergências) | Qtd Avisos | Resumo das Divergências | Pasta

Ordenado por Pontuação (quem tem mais divergência aparece primeiro - fica
fácil ver o que precisa de atenção sem abrir pasta por pasta).

A aba "Consolidado" só lista quem tem divergência ou aviso (quem está OK ou
ainda não foi analisado fica só contado na aba "Resumo" - não polui a
lista). O "Resumo das Divergências" também ficou mais direto: em vez do
nome do arquivo e do texto técnico da IA, mostra "Tipo do documento: motivo
curto" (ex: "CTPS: Em branco / ilegível", "Comprovante de Endereço:
Comprovante vencido (671 dias)"), um por linha.

Como rodar:
    python gerar_consolidado.py

Gera: saidas/relatorio_consolidado.xlsx
"""

from __future__ import annotations

import re
from pathlib import Path

from mvp_v1_validador import (
    PASTA_SAIDAS,
    PASTA_SAIDAS_PRECADASTRO,
    PASTA_SAIDAS_RESERVA,
    carregar_config,
    listar_todos_precadastros,
)
from analisar_documentos import numero_da_pasta_e_idreserva


# Particionamento (29/08/2026): saidas/ agora tem duas subpastas -
# pre_cadastro/ (mvp_v1_validador.py) e reserva/ (mvp_v2_reserva.py).
# Varre as duas, e por retrocompatibilidade também inclui pastas de
# cliente soltas direto em saidas/ (geradas antes do particionamento
# existir). O relatório consolidado em si continua sendo salvo direto
# em saidas/relatorio_consolidado.xlsx (é um relatório único, não é
# uma pasta de cliente, então não faz sentido "particionar" ele).
def listar_pastas_de_cliente() -> list[Path]:
    pastas: list[Path] = []
    for base in (PASTA_SAIDAS_PRECADASTRO, PASTA_SAIDAS_RESERVA):
        if base.is_dir():
            pastas.extend(p for p in base.iterdir() if p.is_dir())
    if PASTA_SAIDAS.is_dir():
        pastas.extend(
            p for p in PASTA_SAIDAS.iterdir()
            if p.is_dir() and p not in (PASTA_SAIDAS_PRECADASTRO, PASTA_SAIDAS_RESERVA)
        )
    return pastas


def extrair_idprecadastro_da_pasta(nome_pasta: str) -> int | None:
    """Pasta é nomeada '{nome do cliente}-{idprecadastro}'. Pega o número
    do final (o nome do cliente pode ter hífen, então não dá pra usar
    split simples - pega só o último pedaço se for todo dígito).

    Em pasta de RESERVA ('...-reserva-{idreserva}') o número devolvido é o
    idRESERVA, não um idprecadastro - quem chama tem que conferir com
    numero_da_pasta_e_idreserva() antes de usá-lo (ver montar_linhas)."""
    m = re.search(r"-(\d+)$", nome_pasta)
    return int(m.group(1)) if m else None


# Traduz as mensagens técnicas do analisar_documentos.py (que citam nome de
# arquivo, similaridade em %, etc.) pra um rótulo curto e direto, do jeito
# que a secretaria bate o olho e já entende. Se aparecer uma mensagem nova
# que não bate em nenhum padrão abaixo, mantém o texto original (fallback,
# não esconde nada, só não fica tão "arrumadinho").
_PADROES_DIVERGENCIA = [
    (re.compile(r"ilegível|má qualidade"), "Em branco / ilegível"),
    # Regra do mês M+3 desde 14/10/2026; a redação antiga ("com N dias") fica
    # para planilhas de rodadas anteriores.
    (re.compile(r"^Comprovante de endereço vencido: .*?(\d{2}/\d{2}/\d{4})"),
     lambda m: f"Comprovante vencido (válido até {m.group(1)})"),
    (re.compile(r"^Comprovante de endereço com (\d+) dias"), lambda m: f"Comprovante vencido ({m.group(1)} dias)"),
    (re.compile(r"^Endereço no comprovante"), "Endereço divergente do cadastro"),
    (re.compile(r"^Não foi possível identificar o endereço"), "Endereço não identificado no documento"),
    (re.compile(r"^Data de emissão do comprovante está no futuro"), "Data de emissão inválida (no futuro)"),
    (re.compile(r"^Data de emissão em formato inesperado"), "Data de emissão não identificada"),
    (re.compile(r"^Não foi possível identificar a data de emissão"), "Sem data de emissão"),
    (re.compile(r"^Nome no documento"), "Nome divergente do cadastro"),
    (re.compile(r"^CPF no documento"), "CPF divergente do cadastro"),
    (re.compile(r"^Não foi possível ler o documento"), "Erro ao ler o documento"),
    (re.compile(r"^Renda (d[oa] .*?) na Tela de Avaliação de Risco .* difere"),
     lambda m: f"Renda {m.group(1)} diferente da Tela de Avaliação de Risco"),
]


def simplificar_divergencia(texto: str) -> str:
    for padrao, rotulo in _PADROES_DIVERGENCIA:
        m = padrao.search(texto)
        if m:
            return rotulo(m) if callable(rotulo) else rotulo
    return texto  # não bateu em nenhum padrão conhecido - mostra como veio


def ler_divergencias_da_pasta(caminho_xlsx: Path) -> dict | None:
    """Lê a aba 'Divergencias (IA)' de um Excel de cliente já processado
    pelo analisar_documentos.py. Retorna None se a pasta ainda não foi
    analisada (aba não existe)."""
    from openpyxl import load_workbook

    wb = load_workbook(caminho_xlsx, data_only=True)
    if "Divergencias (IA)" not in wb.sheetnames:
        return None

    ws = wb["Divergencias (IA)"]
    cabecalho = [c.value for c in ws[1]]
    linhas = []
    for linha in ws.iter_rows(min_row=2, values_only=True):
        if not any(linha):
            continue
        linhas.append(dict(zip(cabecalho, linha)))

    resumo_divergencias = []
    qtd_divergencias = 0
    qtd_avisos = 0
    for linha in linhas:
        div_texto = str(linha.get("divergencias") or "")
        aviso_texto = str(linha.get("avisos") or "")
        if div_texto and div_texto != "OK":
            itens = div_texto.split(" | ")
            qtd_divergencias += len(itens)
            tipo_doc = linha.get("tipo_cv") or linha.get("arquivo", "")
            for item in itens:
                resumo_divergencias.append(f"{tipo_doc}: {simplificar_divergencia(item)}")
        if aviso_texto:
            qtd_avisos += len(aviso_texto.split(" | "))

    return {
        "qtd_divergencias": qtd_divergencias,
        "qtd_avisos": qtd_avisos,
        "resumo": "\n".join(resumo_divergencias) if resumo_divergencias else "",
        "qtd_documentos": len(linhas),
    }


def ler_nome_cliente(caminho_xlsx: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(caminho_xlsx, data_only=True)
    ws = wb["Dados Cadastrais"]
    for linha in ws.iter_rows(min_row=2, values_only=True):
        if linha and linha[0] == "nome":
            return str(linha[1] or "")
    return ""


def montar_linhas(cfg: dict) -> list[dict]:
    print("Buscando dados dos pré-cadastros no CV (empreendimento, unidade, situação)...")
    todos_precadastros = listar_todos_precadastros(cfg)
    # Chave sempre int, dos dois lados (o número da pasta já é int). Defensivo,
    # 09/10/2026: não deu pra confirmar offline se a listagem devolve o
    # idprecadastro como número ou como texto, e com "34243" de um lado e 34243
    # do outro a busca falharia em TODA pasta, em silêncio - o relatório sairia
    # com empreendimento, unidade e situação vazios sem ninguém saber por quê.
    precadastros_por_id = {}
    for p in todos_precadastros:
        try:
            precadastros_por_id[int(p.get("idprecadastro"))] = p
        except (TypeError, ValueError):
            continue
    print(f"  {len(precadastros_por_id)} pré-cadastro(s) encontrados no CV.\n")

    pastas = sorted(listar_pastas_de_cliente(), key=lambda p: p.name)
    linhas = []
    pastas_de_reserva = 0

    for pasta in pastas:
        idprecadastro = extrair_idprecadastro_da_pasta(pasta.name)
        if idprecadastro is None:
            print(f"  {pasta.name}: nome de pasta não segue o padrão '{{nome}}-{{idprecadastro}}', pulando")
            continue

        caminhos_xlsx = list(pasta.glob("*.xlsx"))
        if not caminhos_xlsx:
            print(f"  {pasta.name}: sem .xlsx dentro, pulando")
            continue
        caminho_xlsx = caminhos_xlsx[0]

        if numero_da_pasta_e_idreserva(pasta.name):
            # Pasta de RESERVA: o número do nome é o idRESERVA, e procurá-lo
            # entre os pré-cadastros acha o pré-cadastro de OUTRA pessoa sempre
            # que as duas numerações coincidem (estão na mesma faixa).
            #
            # 09/10/2026, verificação offline do projeto - armadilha 5, a mesma
            # do caso 34239: empreendimento, unidade, situação e "Nº
            # Pré-cadastro" saíam de outra pessoa, com cara de certo. A planilha
            # da reserva não guarda o idprecadastro (a aba Dados Cadastrais vem
            # do cadastro da PESSOA), então esses campos ficam em branco. Dado
            # vazio é melhor que dado de outra pessoa. O nome continua vindo da
            # própria planilha.
            idprecadastro = None
            info_cv = {}
            pastas_de_reserva += 1
        else:
            info_cv = precadastros_por_id.get(idprecadastro, {})
        resultado_analise = ler_divergencias_da_pasta(caminho_xlsx)

        try:
            nome_cliente = ler_nome_cliente(caminho_xlsx)
        except Exception:
            nome_cliente = ""
        if not nome_cliente:
            nome_cliente = info_cv.get("pessoa", "")

        linha = {
            "cliente": nome_cliente,
            "empreendimento": info_cv.get("empreendimento", ""),
            "idprecadastro": idprecadastro,
            "unidade": info_cv.get("unidade", ""),
            "situacao": info_cv.get("situacao", ""),
            "pasta": pasta.name,
        }

        if resultado_analise is None:
            linha["status"] = "Não analisado ainda"
            linha["pontuacao"] = None
            linha["qtd_avisos"] = None
            linha["resumo"] = ""
        else:
            linha["pontuacao"] = resultado_analise["qtd_divergencias"]
            linha["qtd_avisos"] = resultado_analise["qtd_avisos"]
            linha["resumo"] = resultado_analise["resumo"]
            if resultado_analise["qtd_divergencias"] > 0:
                linha["status"] = "Com divergência"
            elif resultado_analise["qtd_avisos"] > 0:
                linha["status"] = "Só aviso"
            else:
                linha["status"] = "OK"

        linhas.append(linha)

    if pastas_de_reserva:
        print(f"  {pastas_de_reserva} pasta(s) de reserva: empreendimento, unidade, situação e "
              f"Nº Pré-cadastro ficam em branco (o número da pasta é o idreserva, não o "
              f"idprecadastro)")
    return linhas


def gerar_excel_consolidado(linhas: list[dict], caminho_saida: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    # Pontuação mais alta primeiro.
    linhas_ordenadas = sorted(linhas, key=lambda linha_atual: -(linha_atual["pontuacao"] or 0))

    # A aba "Consolidado" só mostra quem tem problema (divergência ou
    # aviso) - pedido explícito do João pra facilitar a leitura. OK e "não
    # analisado ainda" continuam contados no Resumo, só não poluem a lista.
    linhas_com_problema = [l for l in linhas_ordenadas if l["status"] in ("Com divergência", "Só aviso")]

    wb = Workbook()

    # --- Aba 1: Resumo ---
    ws_resumo = wb.active
    ws_resumo.title = "Resumo"
    total = len(linhas_ordenadas)
    com_divergencia = sum(1 for linha_atual in linhas_ordenadas if linha_atual["status"] == "Com divergência")
    so_aviso = sum(1 for linha_atual in linhas_ordenadas if linha_atual["status"] == "Só aviso")
    ok = sum(1 for linha_atual in linhas_ordenadas if linha_atual["status"] == "OK")
    nao_analisado = sum(1 for linha_atual in linhas_ordenadas if linha_atual["status"] == "Não analisado ainda")
    for i, (rotulo, valor) in enumerate(
        [
            ("Total de pré-cadastros", total),
            ("Com divergência", com_divergencia),
            ("Só aviso (revisar, não bloqueia)", so_aviso),
            ("OK (sem problema - não aparece na aba Consolidado)", ok),
            ("Ainda não analisados (rode analisar_documentos.py - não aparece na aba Consolidado)", nao_analisado),
        ],
        start=1,
    ):
        ws_resumo.append([rotulo, valor])
        ws_resumo.cell(row=i, column=1).font = Font(bold=True)
    ws_resumo.column_dimensions["A"].width = 62
    ws_resumo.column_dimensions["B"].width = 12

    # --- Aba 2: Consolidado (só quem tem divergência ou aviso) ---
    ws = wb.create_sheet("Consolidado")
    colunas = [
        "Cliente", "Empreendimento", "Nº Pré-cadastro", "Unidade", "Situação",
        "Status", "Pontuação (divergências)", "Qtd Avisos", "Resumo das Divergências", "Pasta",
    ]
    ws.append(colunas)
    for cel in ws[1]:
        cel.font = Font(bold=True, color="FFFFFF")
        cel.fill = PatternFill("solid", fgColor="0E6070")

    for linha_atual in linhas_com_problema:
        ws.append([
            linha_atual["cliente"],
            linha_atual["empreendimento"],
            linha_atual["idprecadastro"],
            linha_atual["unidade"],
            linha_atual["situacao"],
            linha_atual["status"],
            linha_atual["pontuacao"] if linha_atual["pontuacao"] is not None else "-",
            linha_atual["qtd_avisos"] if linha_atual["qtd_avisos"] is not None else "-",
            linha_atual["resumo"],
            linha_atual["pasta"],
        ])
        cor = {
            "Com divergência": "F7E3E5",
            "Só aviso": "FBF1D6",
        }.get(linha_atual["status"])
        if cor:
            for cel in ws[ws.max_row]:
                cel.fill = PatternFill("solid", fgColor=cor)
        ws.cell(row=ws.max_row, column=9).alignment = Alignment(wrap_text=True, vertical="top")

    larguras = [28, 26, 14, 16, 18, 16, 12, 10, 55, 32]
    for idx, largura in enumerate(larguras, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = largura
    ws.freeze_panes = "A2"

    if not linhas_com_problema:
        ws.append(["Nenhum pré-cadastro com divergência ou aviso no momento."])

    wb.save(caminho_saida)


def main() -> None:
    cfg = carregar_config()
    linhas = montar_linhas(cfg)
    if not linhas:
        print("Nenhuma pasta válida encontrada em saidas/. Rode o mvp_v1_validador.py primeiro.")
        return

    caminho_saida = PASTA_SAIDAS / "relatorio_consolidado.xlsx"
    gerar_excel_consolidado(linhas, caminho_saida)

    total = len(linhas)
    com_divergencia = sum(1 for linha_atual in linhas if linha_atual["status"] == "Com divergência")
    print(f"\nConcluído. {total} pré-cadastro(s) no relatório, {com_divergencia} com divergência.")
    print(f"Salvo em: {caminho_saida}")


if __name__ == "__main__":
    main()
