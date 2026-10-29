"""
Varredura de arquivo repetido - muitas reservas, ZERO chamadas de IA
====================================================================

PARA QUE SERVE: medir, em muitas reservas, quantas têm o mesmo arquivo
anexado em campos diferentes.

Caso real que motivou: numa reserva de venda PJ o MESMO PDF foi anexado em
SEIS campos: RG, certidão de estado civil, comprovante de endereço,
comprovante de renda, CTPS e simulação.

⚠️ SOMENTE LEITURA. Não altera absolutamente nada no CV.
⚠️ Os arquivos baixados são APAGADOS logo após o hash (use --manter pra guardar).

Como rodar:
    python varrer_arquivos_repetidos.py "Venda Validada" 30
    python varrer_arquivos_repetidos.py "Venda Validada" 100
    python varrer_arquivos_repetidos.py --todas 200
    python varrer_arquivos_repetidos.py "Venda Validada" 50 --manter
    python varrer_arquivos_repetidos.py "Venda Validada" 50 --listacompleta

Planilha em `saidas/varredura-arquivo-repetido-{status}.xlsx`, abas
"Por reserva", "Resumo" e "Grupos repetidos".


POR QUE ELE É BARATO
--------------------
A checagem é COMPARAÇÃO DE HASH (SHA-256) do conteúdo baixado. Não usa Gemini,
não lê o documento. O custo é só o download. Conferido que o
analisar_documentos.py importa `google.genai` de forma preguiçosa (dentro das
funções), então importar `detectar_arquivo_repetido` daqui não exige chave nem
faz chamada de IA nenhuma.

É a única regra do projeto com essa propriedade, e não é coincidência: ela não
olha o CONTEÚDO de um documento, olha o CONJUNTO.


DECISÃO IMPORTANTE - A COMPARAÇÃO É POR PESSOA, NUNCA ENTRE PESSOAS
-------------------------------------------------------------------
Desde 28/09/2026 a varredura inclui o 2º promissário comprador, avalista e
dependentes - não só o titular. Isso obrigou a acertar uma coisa antes, senão
o número explodiria em falso positivo:

    Casal COMPARTILHA documento legitimamente.

A certidão de casamento é o MESMO papel para os dois cônjuges. O comprovante
de endereço de quem mora junto é o MESMO. Nos dados reais isso aparece o tempo
todo - numa reserva o titular e o 2º comprador têm ambos
"Comprovante de endereço X e Y.pdf"; noutra, ambos têm
"Certidão de casamento Fulano e Beltrana.pdf".

Se o hash fosse comparado entre as pessoas da reserva, TODO casal viraria
apontamento. Por isso cada pessoa é hasheada na sua própria pasta, e o mesmo
arquivo aparecendo no campo X de um e no campo X do outro é ignorado - como
deve ser.

LIMITE CONHECIDO: o mesmo arquivo no campo RG do titular e no campo
Comprovante de Renda do 2º comprador NÃO é pego. É cruzado e de tipo diferente,
mas é raro e ambíguo demais pra tratar sem estudar caso real primeiro. Fica
registrado como lacuna consciente, não como esquecimento.


CLASSIFICAÇÃO EXIGIDO x NÃO EXIGIDO
------------------------------------
Adicionada em 28/09/2026 depois da primeira rodada real. Olhando caso a caso,
a maioria repetia arquivo SOMENTE em campos que a cartilha não exige - CTPS,
Simulação Inicial e Extrato de FGTS concentravam as aparições.

Isso muda a conclusão: na maioria dos casos não é documento falsificado, é
gente empurrando um arquivo qualquer num campo que o CV obriga e o processo
não pede. Tem conserto (parar de obrigar esses campos) e não acusa ninguém.

Tratar o percentual bruto como fraude seria errado e desmontaria no primeiro
questionamento. Por isso a classificação agora é feita PELO SCRIPT, usando a
matriz do próprio analisar_documentos.py, e não à mão depois.

O grupo é marcado GRAVE quando cobre DOIS OU MAIS documentos exigidos - aí não
sobra leitura benigna.

COMPROVANTE DE RENDA: NÃO é exigido (gestora comercial, 14/10/2026). Ficou exigido do
titular em financiamento só entre 13/10 e 14/10/2026, quando o João reverteu a
decisão. Por isso não entra no cálculo de GRAVE em modalidade nenhuma. A coluna
"tem comprov. de renda?" continua na planilha, só como informação.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

from mvp_v1_validador import (
    carregar_config,
    baixar_documentos,
    sanitizar_nome,
    PASTA_SAIDAS,
)
from mvp_v2_reserva import (
    buscar_documentos_reserva,
    buscar_pagina_reservas,
    listar_todas_reservas,
    situacoes_disponiveis,
    # 05/10/2026: a função passou a morar no mvp_v2_reserva.py, porque o
    # processar_reserva_completa() precisa dela também. Uma cópia só.
    documentos_por_grupo,
)
from analisar_documentos import (
    detectar_arquivo_repetido,
    classificar_modalidade,
    documentos_exigidos_da_modalidade,
    documento_satisfaz,
    papel_e_titular,
    EXIGIDOS_SO_DO_TITULAR,
    # Ver a nota de COMPROVANTE DE RENDA no topo do arquivo. Até 13/10/2026
    # havia uma cópia local desta lista; agora é a mesma da matriz.
    TERMOS_COMPROVANTE_DE_RENDA,
)


PASTA_TEMP = PASTA_SAIDAS / "varredura-temp"

CHAVES_CANDIDATAS_CLIENTE = [
    "cliente", "nome_cliente", "cliente_nome", "nome", "titular",
]


def nome_do_cliente(registro: dict, ja_avisou: list[bool]) -> str:
    for chave in CHAVES_CANDIDATAS_CLIENTE:
        valor = registro.get(chave)
        if valor and str(valor).strip():
            return str(valor).strip()
    if not ja_avisou[0]:
        ja_avisou[0] = True
        print()
        print("  (nota: não achei o nome do cliente no registro da reserva.")
        print("   Campos que existem nele:")
        print("   " + ", ".join(sorted(registro.keys())))
        print("   Acrescente o campo certo em CHAVES_CANDIDATAS_CLIENTE.)")
        print()
    return ""


# --------------------------------------------------------------------------
# Listagem: varrer de trás pra frente
# --------------------------------------------------------------------------

def buscar_reservas_recentes(
    cfg: dict, situacao: str | None, quantidade: int,
) -> tuple[list[dict], bool, list[str]]:
    """As N reservas mais recentes da situação pedida, SEM baixar as 55
    páginas da listagem quando não for preciso.

    A listagem de /v1/cvdw/reservas vem em ordem CRESCENTE de idreserva, então
    as recentes estão nas ÚLTIMAS páginas. Buscar de trás pra frente acha as 30
    mais recentes em 2 ou 3 requisições em vez de 55 - e a listagem é, de longe,
    a parte mais lenta da varredura.

    A ordenação crescente é uma SUPOSIÇÃO sobre a API, e ela **não se confirmou**
    (medido em 28/09 e de novo em 06/10/2026). Por isso a função confere a ordem
    nas páginas que leu, e quem chama cai pra listagem completa quando ela não
    bate. Melhor gastar 55 requisições do que varrer as reservas erradas em
    silêncio.

    CORREÇÃO DE 06/10/2026: quando o recorte não existe (status digitado errado,
    por exemplo), o laço percorria as 55 páginas achando zero, só então reprovava
    a ordenação, e o chamador baixava as 55 de novo - 110 requisições pra
    descobrir que o nome do status estava errado. Agora, se o laço chegou a ler
    TODAS as páginas, a ordenação deixa de importar: a listagem completa já está
    na mão, e é ela que volta.

    Devolve (reservas, deu_certo, situacoes_vistas).
    """
    corpo = buscar_pagina_reservas(cfg, 1)
    total_paginas = corpo.get("total_de_paginas") or 1
    print(f"      listagem tem {total_paginas} página(s); indo de trás pra frente")

    alvo = situacao.strip().lower() if situacao else None
    coletadas: list[dict] = []
    todas_lidas: list[dict] = []
    ids_vistos: list[int] = []
    paginas_lidas = 0

    for pagina in range(total_paginas, 0, -1):
        corpo = corpo if pagina == 1 else buscar_pagina_reservas(cfg, pagina)
        itens = corpo.get("dados") or []
        paginas_lidas += 1
        todas_lidas = itens + todas_lidas
        ids_vistos = [i.get("idreserva") or 0 for i in itens] + ids_vistos

        if alvo:
            achadas = [
                r for r in itens
                if (r.get("situacao") or "").strip().lower() == alvo
            ]
        else:
            achadas = list(itens)
        coletadas = achadas + coletadas
        print(f"      página {pagina}: {len(achadas)} no recorte "
              f"(acumulado {len(coletadas)})")

        if len(coletadas) >= quantidade:
            break

    situacoes = sorted({
        (r.get("situacao") or "").strip() for r in todas_lidas if r.get("situacao")
    })

    # Leu tudo? Então a ordenação é irrelevante - a listagem completa está aqui,
    # não há por que baixar de novo.
    if paginas_lidas >= total_paginas:
        coletadas.sort(key=lambda r: r.get("idreserva", 0), reverse=True)
        return coletadas[:quantidade], True, situacoes

    # Paramos cedo: aí sim a ordenação importa, porque as páginas que NÃO foram
    # lidas podem conter reservas mais recentes que as lidas.
    if ids_vistos != sorted(ids_vistos):
        print("      *** A listagem NÃO veio em ordem crescente de idreserva. "
              "A busca de trás pra frente não vale aqui - caindo pra listagem "
              "completa. ***")
        return [], False, situacoes

    coletadas.sort(key=lambda r: r.get("idreserva", 0), reverse=True)
    return coletadas[:quantidade], True, situacoes


# --------------------------------------------------------------------------
# Documentos: TODOS os grupos, não só o titular
# --------------------------------------------------------------------------

def agrupar_por_conteudo(pasta: Path, documentos: list[dict]) -> dict[str, list[str]]:
    """Mesmo agrupamento que detectar_arquivo_repetido faz internamente, mas
    devolvendo os GRUPOS em vez da frase pronta - a planilha precisa saber
    QUAIS campos foram preenchidos com o mesmo arquivo.

    O VEREDITO continua vindo de detectar_arquivo_repetido (fonte única da
    verdade, a mesma função que a auditoria usa). Se as duas contagens
    divergirem, o script avisa - sinal de que a lógica de lá mudou.
    """
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
            nome_cv = (doc.get("nome") or "").strip().lower()
            if not nome_cv:
                continue
            chave = f"nome:{nome_cv}"
        por_conteudo.setdefault(chave, []).append(tipo)
    return por_conteudo


def classificar_grupo(
    tipos: list[str], modalidade: str | None, grupo: str | None = None,
) -> dict:
    """Quais documentos EXIGIDOS pela cartilha este grupo repetido cobre?

    Usa a matriz do próprio analisar_documentos.py - não uma cópia. Cobrir dois
    ou mais exigidos é GRAVE: significa que o mesmo arquivo está fazendo o
    papel de dois documentos que o processo realmente pede.

    'grupo' é a pessoa dona dos arquivos ("titular", "2_ao_promissario_comprador"
    ...). Fora do titular, só saem os EXIGIDOS_SO_DO_TITULAR - lista vazia
    desde 14/10/2026, quando o comprovante de renda deixou de ser exigido.
    """
    exigidos = documentos_exigidos_da_modalidade(modalidade)
    if not papel_e_titular(grupo):
        exigidos = [e for e in exigidos if e not in EXIGIDOS_SO_DO_TITULAR]
    exigidos_cobertos = [
        termos[0]
        for termos in exigidos
        if documento_satisfaz(termos, tipos)
    ]
    tem_renda = documento_satisfaz(TERMOS_COMPROVANTE_DE_RENDA, tipos)
    return {
        "exigidos": exigidos_cobertos,
        "tem_comprovante_de_renda": tem_renda,
        "grave": len(exigidos_cobertos) >= 2,
    }


def varrer_uma_reserva(
    cfg: dict, idreserva: int, modalidade: str | None, manter: bool,
) -> dict:
    """Baixa os documentos de TODAS as pessoas da reserva, hasheia cada uma
    separadamente e devolve o que achou. Não usa IA."""
    dados = buscar_documentos_reserva(cfg, idreserva)
    grupos = documentos_por_grupo(dados)

    pasta_reserva = PASTA_TEMP / str(idreserva)
    try:
        pessoas: list[dict] = []
        total_docs = 0
        total_falhas = 0

        for nome_grupo, docs in grupos.items():
            # Cada pessoa na SUA pasta: é o que impede a certidão de casamento
            # compartilhada pelo casal de virar falso positivo.
            pasta_grupo = pasta_reserva / sanitizar_nome(nome_grupo)
            docs = baixar_documentos(cfg, docs, pasta_grupo)
            total_docs += len(docs)
            total_falhas += sum(1 for d in docs if d.get("erro_download"))

            achados = detectar_arquivo_repetido(pasta_grupo, docs)
            bruto = agrupar_por_conteudo(pasta_grupo, docs)
            repetidos = []
            for chave, tipos in bruto.items():
                distintos = sorted(set(tipos))
                if len(distintos) < 2:
                    continue
                item = {"tipos": distintos, "por_nome": chave.startswith("nome:")}
                item.update(classificar_grupo(distintos, modalidade, nome_grupo))
                repetidos.append(item)

            if len(repetidos) != len(achados):
                print(f"      *** INCONSISTÊNCIA em '{nome_grupo}': "
                      f"detectar_arquivo_repetido achou {len(achados)}, o detalhe "
                      f"daqui achou {len(repetidos)}. Revisar agrupar_por_conteudo(). ***")

            pessoas.append({
                "grupo": nome_grupo,
                "n_documentos": len(docs),
                "repetidos": repetidos,
            })

        return {
            "idreserva": idreserva,
            "modalidade": modalidade or "",
            "pessoas": pessoas,
            "n_pessoas": len(pessoas),
            "tem_segundo_proponente": any(p["grupo"] != "titular" for p in pessoas),
            "n_documentos": total_docs,
            "n_falhas_download": total_falhas,
        }
    finally:
        if not manter:
            shutil.rmtree(pasta_reserva, ignore_errors=True)


# --------------------------------------------------------------------------
# Leitura dos resultados
# --------------------------------------------------------------------------

def rotulo_pessoa(grupo: str) -> str:
    return {
        "titular": "Titular",
        "2_ao_promissario_comprador": "2º Promissário Comprador",
        "3_ao_promissario_comprador": "3º Promissário Comprador",
        "dependente_s": "Dependente(s)",
    }.get(grupo, grupo.replace("_", " ").title())


def pessoas_com_repetido(r: dict) -> list[dict]:
    return [p for p in r["pessoas"] if p["repetidos"]]


def tem_repetido(r: dict) -> bool:
    return bool(pessoas_com_repetido(r))


def tem_grave(r: dict) -> bool:
    return any(g["grave"] for p in r["pessoas"] for g in p["repetidos"])


def maior_grupo(r: dict) -> int:
    return max(
        (len(g["tipos"]) for p in r["pessoas"] for g in p["repetidos"]),
        default=0,
    )


# --------------------------------------------------------------------------
# Planilha
# --------------------------------------------------------------------------

def gravar_planilha(caminho: Path, linhas: list[dict], total: int) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    vermelho = PatternFill("solid", fgColor="F7E3E5")
    amarelo = PatternFill("solid", fgColor="FBF0DA")
    azul = PatternFill("solid", fgColor="0E6070")
    cinza = PatternFill("solid", fgColor="6B6B63")

    def cabecalho(ws, linha=1, cor=azul):
        for cel in ws[linha]:
            cel.font = Font(bold=True, color="FFFFFF")
            cel.fill = cor

    # ---- Aba 1 ----
    ws = wb.active
    ws.title = "Por reserva"
    ws.append([
        "idreserva", "cliente", "empreendimento", "modalidade", "pessoas na operação",
        "tem 2º proponente?", "arquivo repetido?", "GRAVE (2+ exigidos)?",
        "quem repetiu", "maior repetição (campos)",
    ])
    cabecalho(ws)
    for r in linhas:
        quem = ", ".join(rotulo_pessoa(p["grupo"]) for p in pessoas_com_repetido(r))
        ws.append([
            r["idreserva"], r["cliente"], r["empreendimento"], r["modalidade"],
            r["n_pessoas"],
            "SIM" if r["tem_segundo_proponente"] else "não",
            "SIM" if tem_repetido(r) else "não",
            "SIM" if tem_grave(r) else "",
            quem, maior_grupo(r) or "",
        ])
        if tem_grave(r):
            for cel in ws[ws.max_row]:
                cel.fill = vermelho
        elif tem_repetido(r):
            for cel in ws[ws.max_row]:
                cel.fill = amarelo
    for i, w in enumerate([12, 30, 24, 22, 18, 18, 18, 20, 34, 22], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ---- Aba 2 ----
    com = [r for r in linhas if tem_repetido(r)]
    graves = [r for r in linhas if tem_grave(r)]
    com_2o = [r for r in linhas if r["tem_segundo_proponente"]]
    so_titular = [r for r in com
                  if all(p["grupo"] == "titular" for p in pessoas_com_repetido(r))]
    so_outros = [r for r in com
                 if all(p["grupo"] != "titular" for p in pessoas_com_repetido(r))]

    def pct(n):
        return f"{n / total * 100:.0f}%" if total else "-"

    ws2 = wb.create_sheet("Resumo")
    ws2.append(["Varredura de arquivo repetido - operação inteira"])
    ws2["A1"].font = Font(bold=True, size=14)
    ws2.append([])
    for rotulo, n in [
        ("Reservas varridas", total),
        ("Com 2º proponente / dependente na operação", len(com_2o)),
        ("Com o mesmo arquivo em mais de um campo (qualquer pessoa)", len(com)),
        ("   ... só no titular", len(so_titular)),
        ("   ... só em quem não é o titular", len(so_outros)),
        ("   ... nos dois", len(com) - len(so_titular) - len(so_outros)),
        ("GRAVE: mesmo arquivo cobrindo 2+ documentos EXIGIDOS", len(graves)),
    ]:
        ws2.append([rotulo, n, pct(n) if rotulo != "Reservas varridas" else ""])
    ws2.append([])
    ws2.append(["Nenhuma chamada de IA foi usada - a checagem é hash SHA-256 do conteúdo."])
    ws2.append(["A comparação é feita DENTRO de cada pessoa: casal compartilha certidão"])
    ws2.append(["de casamento e comprovante de endereço legitimamente."])
    ws2.append([])
    ws2.append(["Campos mais preenchidos com arquivo repetido", "aparições", "a cartilha exige?"])
    cabecalho(ws2, ws2.max_row, cinza)

    freq: Counter = Counter()
    exigido_por_tipo: dict[str, bool] = {}
    for r in linhas:
        for p in r["pessoas"]:
            for g in p["repetidos"]:
                for t in g["tipos"]:
                    freq[t] += 1
                    exigido_por_tipo.setdefault(t, False)
                for e in g["exigidos"]:
                    pass
    # marca exigência consultando a matriz da modalidade de cada reserva
    for r in linhas:
        mod = classificar_modalidade(r["modalidade"])
        for t in list(exigido_por_tipo):
            if any(documento_satisfaz(termos, [t])
                   for termos in documentos_exigidos_da_modalidade(mod)):
                exigido_por_tipo[t] = True
    for t, n in freq.most_common():
        ws2.append([t, n, "EXIGIDO" if exigido_por_tipo.get(t) else "não exigido"])
    for i, w in enumerate([56, 14, 20], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    # ---- Aba 3 ----
    ws3 = wb.create_sheet("Grupos repetidos")
    ws3.append([
        "idreserva", "cliente", "pessoa", "quantos campos", "campos",
        "documentos EXIGIDOS cobertos", "GRAVE?", "tem comprov. de renda?",
        "como foi detectado",
    ])
    cabecalho(ws3)
    for r in linhas:
        for p in r["pessoas"]:
            for g in p["repetidos"]:
                ws3.append([
                    r["idreserva"], r["cliente"], rotulo_pessoa(p["grupo"]),
                    len(g["tipos"]), ", ".join(g["tipos"]),
                    ", ".join(g["exigidos"]) or "(nenhum)",
                    "SIM" if g["grave"] else "",
                    "sim" if g["tem_comprovante_de_renda"] else "",
                    "mesmo nome de arquivo" if g["por_nome"] else "conteúdo idêntico",
                ])
                if g["grave"]:
                    for cel in ws3[ws3.max_row]:
                        cel.fill = vermelho
    for i, w in enumerate([12, 28, 26, 14, 56, 38, 10, 20, 22], start=1):
        ws3.column_dimensions[get_column_letter(i)].width = w

    for aba in (ws, ws2, ws3):
        for linha in aba.iter_rows():
            for cel in linha:
                cel.alignment = Alignment(vertical="top", wrap_text=True)

    wb.save(caminho)


# --------------------------------------------------------------------------

def varrer(
    situacao: str | None, quantidade: int,
    manter: bool = False, lista_completa: bool = False,
) -> None:
    cfg = carregar_config()

    rotulo = situacao or "TODAS as situações"
    print("=" * 72)
    print(f"VARREDURA DE ARQUIVO REPETIDO - {quantidade} reserva(s), {rotulo}")
    print("Operação inteira (titular + 2º proponente + dependentes).")
    print("Somente leitura. Nenhuma chamada de IA - só download e hash.")
    print("=" * 72)
    print()

    amostra: list[dict] = []
    situacoes_vistas: list[str] = []
    varreu_tudo = False
    if not lista_completa:
        print("Buscando as reservas mais recentes...")
        amostra, deu_certo, situacoes_vistas = buscar_reservas_recentes(
            cfg, situacao, quantidade)
        varreu_tudo = deu_certo

    # Nome de status que não bate com nada: a listagem já foi lida inteira,
    # então o erro pode ser dito AGORA, sem baixar as 55 páginas de novo.
    if not amostra and varreu_tudo and situacao:
        print(f"\nNenhuma reserva com situação '{situacao}'.")
        print("Situações que existem no seu CV agora:")
        for s_ in situacoes_vistas:
            print(f"  - {s_}")
        return

    if not amostra:
        print("Buscando a lista completa de reservas (demora um pouco)...")
        todas = listar_todas_reservas(cfg)
        if situacao:
            alvo = situacao.strip().lower()
            selecionadas = [r for r in todas
                            if (r.get("situacao") or "").strip().lower() == alvo]
            if not selecionadas:
                print(f"\nNenhuma reserva com situação '{situacao}'.")
                print("Situações que existem no seu CV agora:")
                for s in situacoes_disponiveis(todas):
                    print(f"  - {s}")
                return
        else:
            selecionadas = list(todas)
        selecionadas.sort(key=lambda r: r.get("idreserva", 0), reverse=True)
        amostra = selecionadas[:quantidade]

    if not amostra:
        print(f"\nNenhuma reserva com situação '{situacao}'.")
        return

    print(f"\nVou varrer {len(amostra)} reserva(s), da {amostra[0].get('idreserva')} "
          f"à {amostra[-1].get('idreserva')}.")
    if not manter:
        print("Os arquivos baixados são apagados logo após o hash (--manter guarda).")
    print()

    linhas: list[dict] = []
    falhas: list[tuple[int, str]] = []
    ja_avisou = [False]

    for i, item in enumerate(amostra, start=1):
        idreserva = item.get("idreserva")
        print(f"[{i}/{len(amostra)}] reserva {idreserva} ...", flush=True)
        try:
            modalidade = item.get("tipovenda") or ""
            r = varrer_uma_reserva(
                cfg, idreserva, classificar_modalidade(modalidade), manter)
        except Exception as e:
            print(f"      FALHOU: {e}")
            falhas.append((idreserva, str(e)))
            continue

        r["modalidade"] = modalidade
        r["cliente"] = nome_do_cliente(item, ja_avisou)
        r["empreendimento"] = item.get("empreendimento", "")
        r["situacao"] = item.get("situacao", "")
        linhas.append(r)

        if r["n_pessoas"] > 1:
            outros = [rotulo_pessoa(p["grupo"]) for p in r["pessoas"]
                      if p["grupo"] != "titular"]
            print(f"      operação com {r['n_pessoas']} pessoas: {', '.join(outros)}")
        for p in pessoas_com_repetido(r):
            for g in p["repetidos"]:
                marca = "  [GRAVE]" if g["grave"] else ""
                print(f"      >>> {rotulo_pessoa(p['grupo'])}: mesmo arquivo em "
                      f"{len(g['tipos'])} campo(s){marca}")
                print(f"          {', '.join(g['tipos'])}")
                if g["exigidos"]:
                    print(f"          exigidos cobertos: {', '.join(g['exigidos'])}")

    if not linhas:
        print("\nNenhuma reserva pôde ser varrida.")
        return

    if not manter:
        shutil.rmtree(PASTA_TEMP, ignore_errors=True)

    total = len(linhas)
    com = [r for r in linhas if tem_repetido(r)]
    graves = [r for r in linhas if tem_grave(r)]
    com_2o = [r for r in linhas if r["tem_segundo_proponente"]]
    so_titular = [r for r in com
                  if all(p["grupo"] == "titular" for p in pessoas_com_repetido(r))]

    def linha_pct(rotulo, n):
        print(f"  {rotulo:58} {n:3} de {total}  ({n / total * 100:3.0f}%)")

    print()
    print("=" * 72)
    print(f"RESULTADO - {total} reserva(s) varrida(s) ({rotulo})")
    print("=" * 72)
    print()
    linha_pct("Com 2º proponente / dependente na operação", len(com_2o))
    linha_pct("Com o mesmo arquivo em mais de um campo", len(com))
    linha_pct("   ... só no titular (o que as rodadas antigas viam)", len(so_titular))
    linha_pct("GRAVE: mesmo arquivo cobrindo 2+ documentos EXIGIDOS", len(graves))

    if graves:
        print()
        print("  OS CASOS GRAVES:")
        for r in graves:
            print(f"    {r['idreserva']}  {(r['cliente'] or '')[:30]}")
            for p in r["pessoas"]:
                for g in p["repetidos"]:
                    if g["grave"]:
                        print(f"        {rotulo_pessoa(p['grupo'])}: {', '.join(g['tipos'])}")
                        print(f"        exigidos cobertos: {', '.join(g['exigidos'])}")

    outros_casos = [r for r in com if not tem_grave(r)]
    if outros_casos:
        print()
        print("  Demais casos (repetição só em campos que a cartilha não exige):")
        for r in outros_casos:
            quem = ", ".join(rotulo_pessoa(p["grupo"]) for p in pessoas_com_repetido(r))
            print(f"    {r['idreserva']}  {(r['cliente'] or '')[:28]:28} ({quem})")
            for p in pessoas_com_repetido(r):
                for g in p["repetidos"]:
                    print(f"        {', '.join(g['tipos'])}")

    freq: Counter = Counter()
    for r in linhas:
        for p in r["pessoas"]:
            for g in p["repetidos"]:
                for t in g["tipos"]:
                    freq[t] += 1
    if freq:
        print()
        print("  Campos que mais aparecem em repetição:")
        for t, n in freq.most_common(10):
            print(f"    {n:3}x  {t}")

    problemas = [r for r in linhas if r["n_falhas_download"]]
    if problemas:
        print()
        print(f"  {len(problemas)} reserva(s) com documento que não baixou:")
        for r in problemas[:10]:
            print(f"    - {r['idreserva']}: {r['n_falhas_download']} de {r['n_documentos']}")

    if falhas:
        print()
        print(f"  {len(falhas)} reserva(s) falharam por completo:")
        for idr, erro in falhas[:10]:
            print(f"    - {idr}: {erro[:100]}")

    sufixo = re.sub(r"[^\w\-]+", "-", (situacao or "todas").strip().lower()).strip("-")
    caminho = PASTA_SAIDAS / f"varredura-arquivo-repetido-{sufixo}.xlsx"
    gravar_planilha(caminho, linhas, total)
    print()
    print(f"Planilha: {caminho}")
    print("Abas: 'Por reserva', 'Resumo' e 'Grupos repetidos'")


def main() -> None:
    argv = sys.argv[1:]
    manter = "--manter" in argv
    todas = "--todas" in argv
    lista_completa = "--listacompleta" in argv
    argv = [a for a in argv if not a.startswith("--")]

    if todas:
        situacao = None
        quantidade = int(argv[0]) if argv and argv[0].isdigit() else 30
    elif argv:
        situacao = argv[0]
        quantidade = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 30
    else:
        situacao = input("Situação da reserva (ex: Venda Validada, vazio = todas): ").strip()
        situacao = situacao or None
        entrada = input("Quantas reservas varrer? (padrão 30): ").strip()
        quantidade = int(entrada) if entrada.isdigit() else 30

    varrer(situacao, quantidade, manter=manter, lista_completa=lista_completa)


if __name__ == "__main__":
    main()
