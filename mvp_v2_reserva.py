"""
MVP v2 - Reserva - Secretaria de Vendas
========================================

Mesma lógica do mvp_v1_validador.py, mas trabalhando em cima da RESERVA
(venda já confirmada) em vez do pré-cadastro:

  1) GET /v1/comercial/reservas/{idReserva}/documentos
     -> pega os links dos documentos anexados + o idpessoa_cv do titular
  2) GET /v1/cadastros/clientes?idcliente={idpessoa_cv}
     -> pega o que está de fato cadastrado no CV (dados do titular)
  3) Salva tudo em saidas/reserva/{nome do cliente}-reserva-{idreserva}/:
     - os documentos baixados
     - um Excel com os dados cadastrais + a checklist de documentos

Particionamento (29/08/2026): dentro de saidas/, este script grava
sempre em "reserva/" - o mvp_v1_validador.py (pré-cadastro) grava em
"pre_cadastro/". Os dois tipos não se misturam mais na mesma pasta.

O que este script NÃO faz:
  - Comparar dado a dado (isso é o "encontrar erros" - analisar_documentos.py,
    que também funciona em cima destas pastas de saída)

Atualização (09/10/2026): o fluxo da reserva COMPLETA (titular + 2º
proponente + avalista, uma pasta por pessoa) é processar_reserva_completa(),
mais abaixo neste arquivo. O trecho de cima descreve o MVP original, que só
tratava o titular - o campo 'idtipo_associacao' dos documentos da reserva
não diferencia associados, por isso o casamento grupo x pessoa é feito pelo
nome do grupo (ver docs/MANUAL-TECNICO.md, armadilhas).

Como rodar:
    python mvp_v2_reserva.py                  -> pergunta no terminal
    python mvp_v2_reserva.py 33609             -> processa só essa reserva
    python mvp_v2_reserva.py --situacao "Aguardando Assinaturas/Impressão Boleto"
                                                -> processa TODAS as reservas com essa situação

O modo "por situação" usa GET /v1/cvdw/reservas (paginado) pra listar
todas as reservas do CV, filtra pelo texto da situação (exatamente como
aparece no CV, sem diferenciar maiúsculas de minúsculas) e roda o mesmo
fluxo de cima em cada uma, uma por vez. Se uma reserva falhar, as outras
continuam - no final sai um resumo com quantas deram certo e quais
falharam.

Configuração: reaproveita o mesmo config.json / variáveis de ambiente do
mvp_v1_validador.py (mesmo domínio, email, token).
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import requests

from mvp_v1_validador import (
    carregar_config,
    base_url,
    headers_v1,
    requisicao_com_retry,
    sanitizar_nome,
    extensao_do_link,
    baixar_documentos,
    buscar_dados_cliente,
    gerar_excel,
    PASTA_SAIDAS_RESERVA,
)

# --------------------------------------------------------------------------
# Passo 1: documentos da reserva
# --------------------------------------------------------------------------

class DocumentosIndisponiveisNoCV(SystemExit):
    """O CV não entrega a lista de documentos da reserva: 422 "Ocorreu um erro
    inesperado" ou 200 sem JSON ("Arquivo não encontrado."). 'permanente' só
    quando o diagnóstico CONFIRMA o arquivo ausente (o pré-cadastro também
    responde "Arquivo não encontrado."): aí não passa tentando de novo no mesmo
    dia, e a repescagem não repete. Sem confirmação (um 422 de "tente mais
    tarde" passageiro, HTML temporário, resposta vazia), é falha comum.

    Visto todo dia desde 13/10/2026 em 34441 e 34442 (investigado com GET em
    20/10): as duas são do pré-cadastro 34464, e /precadastro/34464/documentos
    e /reservas/34440/documentos (a 3ª reserva dele, cancelada) devolvem 200
    text/html "Arquivo não encontrado." - um documento desse pré-cadastro
    aponta para um arquivo que não existe mais no CV. /reservas/{id} responde
    200 normal. Outros pré-cadastros com várias reservas funcionam. Só o
    suporte do CV corrige."""

    def __init__(self, idreserva: int, status: int, corpo: str, motivo: str, permanente: bool = False):
        self.idreserva, self.status, self.corpo, self.motivo = idreserva, status, corpo, motivo
        self.permanente = permanente
        super().__init__(f"{motivo} (reserva {idreserva}, HTTP {status}: {corpo[:200]})")


def _documentos_indisponiveis(cfg: dict, idreserva: int, status: int, corpo: str) -> DocumentosIndisponiveisNoCV:
    """Monta a falha com uma frase curta para o log e o painel. Confere (só
    GET) se o pré-cadastro da reserva acusa arquivo ausente, o caso conhecido:
    só ele vira 'permanente'."""
    falha = DocumentosIndisponiveisNoCV(
        idreserva, status, corpo, f"CV não entrega os documentos (HTTP {status}, erro do lado do CV)")
    idprecadastro = (buscar_reserva_individual(cfg, idreserva) or {}).get("idprecadastro")
    if not idprecadastro:
        return falha
    try:
        resp = requests.get(f"{base_url(cfg)}/v1/comercial/precadastro/{idprecadastro}/documentos",
                            headers=headers_v1(cfg), timeout=30)
    except requests.RequestException:
        return falha
    if resp.status_code == 200 and resp.text.strip().lower().startswith("arquivo não encontrado"):
        return DocumentosIndisponiveisNoCV(
            idreserva, status, corpo,
            f"CV não entrega os documentos - arquivo ausente no CV, pré-cadastro {idprecadastro} "
            f"(HTTP {status}; só o suporte do CV corrige)", permanente=True)
    return falha


def buscar_documentos_reserva(cfg: dict, idreserva: int) -> dict:
    """GET /v1/comercial/reservas/{idReserva}/documentos

    Retorna o objeto 'dados' inteiro (não só os documentos), porque é lá
    que vêm idpessoa_cv, idempreendimento_cv, idunidade_cv etc.

    Estrutura confirmada empiricamente (29/08/2026, reserva 33609):
    'documentos' é um DICIONÁRIO agrupado por papel da pessoa
    (só "titular" apareceu até agora - o campo 'idtipo_associacao' de
    cada documento veio sempre None nos testes, não diferencia
    associados). Cada item de cada grupo tem: idreservasdocumentos,
    nome, tipo, situacao, motivo_reprovacao, descricao_motivo_reprovacao,
    idtipo, idtipo_associacao, link.
    """
    url = f"{base_url(cfg)}/v1/comercial/reservas/{idreserva}/documentos"
    resp = requests.get(url, headers=headers_v1(cfg), timeout=30)

    if resp.status_code == 204:
        raise SystemExit(f"Reserva {idreserva}: não há documentos cadastrados.")
    if resp.status_code == 422:
        raise _documentos_indisponiveis(cfg, idreserva, 422, resp.text)
    if resp.status_code != 200:
        raise SystemExit(
            f"Erro {resp.status_code} ao buscar documentos da reserva "
            f"{idreserva}: {resp.text[:500]}"
        )

    try:
        corpo = resp.json()
    except ValueError:  # 200 text/html "Arquivo não encontrado." (34440)
        raise _documentos_indisponiveis(cfg, idreserva, 200, resp.text)
    dados = corpo.get("dados")

    # 'dados' pode vir como lista (um item por reserva) ou como dict direto
    # - mesmo comportamento variável já visto no endpoint de pré-cadastro.
    if isinstance(dados, list):
        if not dados:
            raise SystemExit(f"Resposta sem 'dados' para a reserva {idreserva}: {corpo}")
        return dados[0]
    if isinstance(dados, dict):
        return dados

    raise SystemExit(f"Formato inesperado de 'dados' para a reserva {idreserva}: {corpo}")


def documentos_do_titular_reserva(dados_reserva: dict) -> list[dict]:
    """Extrai a lista de documentos do titular, deduplicada por tipo
    (mantendo o de maior id = mais recente). Mesma regra do v1, adaptada
    para o campo idreservasdocumentos (em vez de idprecadastrosdocumentos)
    e para o link vir em 'link' (não 'link_download')."""
    grupos = dados_reserva.get("documentos") or {}

    # NUNCA descartar grupo em silêncio. Até 22/09/2026 esta função pegava só
    # a chave "titular" e jogava fora o resto sem dizer nada - e o projeto
    # tinha concluído que "só existe titular" com base em DUAS reservas: a
    # 33609 (loteamento, todos os PDFs em branco) e o pré-cadastro 33998.
    # Nenhuma das duas foi testada com um 2º Promissário Comprador real.
    #
    # Como a reserva 33937 tem uma 2ª compradora (confirmado via
    # /v1/cvdw/reservas/associados), se o CV agrupar os documentos dela numa
    # chave própria, ela apareceria aqui - e estaria sendo descartada sem
    # ninguém saber. Este aviso transforma a suposição em medição.
    outros_grupos = [g for g in grupos.keys() if g != "titular"]
    if outros_grupos:
        print(
            f"      *** ATENÇÃO: o CV devolveu grupo(s) de documento além do "
            f"titular: {outros_grupos} ***"
        )
        for grupo in outros_grupos:
            itens = grupos.get(grupo) or []
            print(f"          '{grupo}': {len(itens)} documento(s)")
            for doc in itens[:10]:
                print(f"            - {doc.get('tipo', '?')}: {doc.get('nome', '?')}")
        print(
            "          Esses documentos NÃO estão sendo baixados nem validados "
            "hoje - só o titular é processado."
        )

    docs_titular = grupos.get("titular") or []

    mais_recente_por_tipo: dict[str, dict] = {}
    for doc in docs_titular:
        tipo = doc.get("tipo") or doc.get("idtipo")
        atual = mais_recente_por_tipo.get(tipo)
        id_doc = doc.get("idreservasdocumentos", 0)
        if atual is None or id_doc > atual.get("idreservasdocumentos", 0):
            mais_recente_por_tipo[tipo] = doc

    return list(mais_recente_por_tipo.values())


# --------------------------------------------------------------------------
# Passo 0 (opcional): listar reservas por situação
# --------------------------------------------------------------------------

def buscar_pagina_reservas(cfg: dict, pagina: int, por_pagina: int = 500) -> dict:
    """GET /v1/cvdw/reservas - lista paginada de reservas (CVDW, mesma
    autenticação email+token). Cada item já vem com 'idreserva',
    'idprecadastro', 'idpessoa_cv'/'idpessoa' e 'situacao' (texto)."""
    url = f"{base_url(cfg)}/v1/cvdw/reservas"
    params = {"pagina": pagina, "registros_por_pagina": por_pagina}
    resp = requisicao_com_retry(
        url, headers=headers_v1(cfg), params=params,
        contexto=f"listagem de reservas, página {pagina}")
    if resp.status_code != 200:
        raise SystemExit(
            f"Erro {resp.status_code} ao listar reservas (página {pagina}): "
            f"{resp.text[:500]}"
        )
    return resp.json()


def buscar_reserva_individual(cfg: dict, idreserva: int) -> dict | None:
    """GET /v1/comercial/reservas/{idReserva} - UMA reserva, sem paginação.

    Existe pra evitar o absurdo de baixar as ~27 mil reservas do CV (54
    páginas, medido em 22/09/2026) só pra descobrir o 'tipovenda' de uma.
    Devolve None se o endpoint falhar ou não trouxer o registro - nesse caso
    o chamador cai na varredura paginada, que sempre funcionou.

    Só pode ser chamada com um idRESERVA de verdade. Passar um idprecadastro
    aqui traria a reserva ERRADA (o número existiria como id de outra
    reserva), por isso o chamador confere o formato do nome da pasta antes, e
    esta função ainda valida o idreserva que voltou.
    """
    url = f"{base_url(cfg)}/v1/comercial/reservas/{idreserva}"
    try:
        resp = requests.get(url, headers=headers_v1(cfg), timeout=30)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        corpo = resp.json()
    except ValueError:
        return None

    dados = corpo.get("dados", corpo) if isinstance(corpo, dict) else corpo
    if isinstance(dados, list):
        dados = dados[0] if dados else None
    if not isinstance(dados, dict):
        return None

    # O endpoint devolve o registro DENTRO de uma chave com o próprio número:
    # {"34424": {"tipovenda": ..., "idprecadastro": ...}}. Repassado assim, o
    # chamador não achava 'tipovenda' no topo e caía SEMPRE na varredura
    # paginada - a busca direta nunca tinha funcionado (achado na construção
    # do painel, 13/10/2026). A chave é a conferência do idreserva: veio outro
    # número, é outra reserva, e aí melhor cair na varredura.
    if len(dados) == 1:
        chave, valor = next(iter(dados.items()))
        if str(chave).isdigit() and isinstance(valor, dict):
            if str(chave) != str(idreserva):
                return None
            dados = valor

    # Confere que veio mesmo a reserva pedida - se o endpoint devolver outra
    # coisa, melhor cair na varredura do que usar dado errado.
    id_voltou = dados.get("idreserva") or dados.get("idReserva")
    if id_voltou is not None and str(id_voltou) != str(idreserva):
        return None
    return dados


def buscar_associados_da_reserva(cfg: dict, idreserva: int) -> list[dict]:
    """GET /v1/cvdw/reservas/associados - quem são as OUTRAS pessoas da
    reserva (2º/3º/4º Promissário Comprador, Avalista 1, Avalista 2).

    Resolvido em 22/09/2026, depois de meses em aberto. O projeto tinha
    concluído que só o endpoint v3 (que exige login com senha) responderia
    isso, porque duas hipóteses foram testadas e descartadas. Este terceiro
    candidato estava no mapa de endpoints desde agosto e nunca tinha sido
    medido - e funciona com a MESMA autenticação email+token do resto do
    projeto. A questão do Bearer/senha deixou de existir para este fluxo.

    Campos confirmados num teste real: idassociado, idreserva, idpessoa,
    idconjuge, nome, documento (CPF), renda_familiar, tipo_associacao,
    ativo, data_cad.

    Devolve SÓ os associados ativos (ativo == "S"). Na amostra real, 3 de 30
    registros vinham com ativo="N" - são associados removidos da operação, e
    tratá-los como atuais faria o sistema cobrar documento de gente que não
    participa mais.
    """
    # A listagem é grande (6.791 associados em 227 páginas de 30 na medição
    # de 22/09/2026) e é lida INTEIRA, uma vez por processo - ver
    # _listar_todos_os_associados().
    encontrados = [
        item for item in _listar_todos_os_associados(cfg)
        if item.get("idreserva") == idreserva
    ]

    ativos = [a for a in encontrados if (a.get("ativo") or "S").upper() == "S"]
    inativos = len(encontrados) - len(ativos)
    if inativos:
        print(f"      ({inativos} associado(s) inativo(s) ignorado(s))")
    return ativos


# Listagem completa de /v1/cvdw/reservas/associados, por domínio do CV, lida
# uma vez por processo. Só entra aqui listagem lida até a ÚLTIMA página.
_LISTAGEM_DE_ASSOCIADOS: dict[str, list[dict]] = {}


def _listar_todos_os_associados(cfg: dict) -> list[dict]:
    """Todas as páginas da listagem de associados, sem parada antecipada.

    09/10/2026, verificação offline do projeto (armadilha 6). A versão anterior
    varria do fim para o começo e parava na primeira página sem associado novo,
    supondo a listagem em ordem crescente de idassociado e os associados de uma
    reserva JUNTOS. Nenhuma das duas suposições se sustenta: a armadilha 6 já
    mediu que a listagem do CV não vem ordenada, e mesmo ordenada, um associado
    incluído DEPOIS na reserva ganha idassociado alto e cai páginas longe do
    primeiro. Com páginas falsas: a reserva com o 2º comprador na página 2 e um
    avalista incluído depois na página 5 devolvia só o avalista (lidas [1, 5, 4])
    - o 2º comprador sumia, não era validado, e os documentos dele viravam
    divergência de nome/CPF na pasta do titular. E os associados na virada da
    página 1 para a 2 se perdiam (lidas [1, 4]).

    Parar quando os achados "cobrirem os grupos de documento" não serve: falha
    justamente com associado SEM documento, que também tem que aparecer.

    Ler tudo custa mais por consulta, por isso a lista fica em memória e várias
    reservas na mesma execução (testar_amostra, varredura, --todas) pagam a
    leitura uma vez só. Se QUALQUER página falhar, levanta SystemExit e nada é
    guardado: lista parcial aqui é exatamente o erro que esta função corrige.
    A próxima chamada tenta de novo, do começo.
    """
    url = f"{base_url(cfg)}/v1/cvdw/reservas/associados"
    if url in _LISTAGEM_DE_ASSOCIADOS:
        return _LISTAGEM_DE_ASSOCIADOS[url]

    def pagina(n: int) -> dict:
        resp = requisicao_com_retry(
            url, headers=headers_v1(cfg),
            params={"pagina": n, "registros_por_pagina": 500},
            contexto=f"associados, página {n}")
        if resp.status_code != 200:
            raise SystemExit(
                f"Erro {resp.status_code} ao listar associados (página {n}): "
                f"{resp.text[:300]}"
            )
        return resp.json()

    corpo = pagina(1)
    total_paginas = corpo.get("total_de_paginas", 1) or 1
    todos: list[dict] = list(corpo.get("dados") or [])
    for n in range(2, total_paginas + 1):
        todos.extend(pagina(n).get("dados") or [])

    _LISTAGEM_DE_ASSOCIADOS[url] = todos
    return todos


def procurar_reserva_na_listagem(cfg: dict, campo: str, valor) -> dict | None:
    """Procura UM registro em /v1/cvdw/reservas varrendo as páginas DE TRÁS
    PRA FRENTE.

    Motivo (medido em 22/09/2026): a listagem tem 54 páginas de 500 registros
    (~27 mil reservas), e uma reserva recente - justamente o caso de uso normal
    aqui - costuma estar nas ÚLTIMAS páginas. Varrer do começo, como se fazia
    antes, percorria quase tudo. Varrendo do fim, uma reserva nova aparece em 1
    ou 2 requisições.

    A ordem é só um ATALHO de velocidade, não premissa de correção: a listagem
    NÃO vem garantidamente ordenada (armadilha 6, medido duas vezes), e por isso
    esta função não para antes de achar - sem achar, lê todas as páginas.
    Corrigido em 09/10/2026: o texto dizia "vem em ordem crescente".

    Devolve o registro ou None. Não monta lista nenhuma na memória - só
    procura."""
    corpo = buscar_pagina_reservas(cfg, 1)
    total_paginas = corpo.get("total_de_paginas", 1)

    for item in (corpo.get("dados") or []):
        if item.get(campo) == valor:
            print(f"      achei {campo}={valor} na página 1")
            return item

    for pagina in range(total_paginas, 1, -1):
        corpo = buscar_pagina_reservas(cfg, pagina)
        itens = corpo.get("dados") or []
        print(f"      procurando {campo}={valor}: página {pagina}/{total_paginas}")
        for item in itens:
            if item.get(campo) == valor:
                print(f"      achei na página {pagina}")
                return item
    return None


def listar_todas_reservas(cfg: dict, parar_em: dict | None = None) -> list[dict]:
    """Percorre as páginas de /v1/cvdw/reservas.

    'parar_em' permite sair assim que o registro procurado aparecer, em vez
    de baixar a lista inteira - ex: {"idreserva": 33937}. Sem ele, baixa
    tudo (comportamento original, que o modo --situacao precisa).
    """
    pagina = 1
    todas: list[dict] = []
    while True:
        corpo = buscar_pagina_reservas(cfg, pagina)
        itens = corpo.get("dados") or []
        todas.extend(itens)
        total_paginas = corpo.get("total_de_paginas", pagina)
        print(f"      página {pagina}/{total_paginas} ({len(itens)} registros)")

        if parar_em:
            campo, valor = next(iter(parar_em.items()))
            if any(item.get(campo) == valor for item in itens):
                print(f"      achei {campo}={valor} nesta página - parando a varredura aqui")
                return todas

        if pagina >= total_paginas or not itens:
            break
        pagina += 1
    return todas


def casar_situacao(pedida: str, itens: list[dict]) -> tuple[str | None, list[str]]:
    """Acha a situação que o usuário quis dizer. Devolve (situacao_exata, opcoes).

    Digitar o nome exato de um status com acento e barra
    ("Aguardando Assinaturas/Impressão Boleto") é pedir erro de digitação, e
    cada erro custa a listagem inteira - 55 páginas - antes de a ferramenta
    dizer que não achou. Aconteceu duas vezes em 06/10/2026.

    A regra, em ordem:
      1. igual (ignorando caixa, acento e espaço sobrando);
      2. um único status que CONTÉM o que foi digitado - "aguardando validacao"
         acha "Contrato Aguardando Validação";
      3. mais de um candidato: devolve None e a lista deles, pra quem chama
         mostrar e não escolher no chute.
    """
    def norm(t: str) -> str:
        t = unicodedata.normalize("NFKD", str(t or ""))
        t = "".join(c for c in t if not unicodedata.combining(c))
        return re.sub(r"\s+", " ", t).strip().lower()

    todas = situacoes_disponiveis(itens)
    alvo = norm(pedida)
    if not alvo:
        return None, todas

    for s in todas:
        if norm(s) == alvo:
            return s, todas

    contem = [s for s in todas if alvo in norm(s)]
    if len(contem) == 1:
        return contem[0], todas
    if len(contem) > 1:
        return None, contem
    return None, todas


def situacoes_disponiveis(itens: list[dict]) -> list[str]:
    return sorted({(item.get("situacao") or "").strip() for item in itens if item.get("situacao")})


def processar_por_situacao(situacao_alvo: str) -> None:
    """Busca TODAS as reservas, filtra pela situação informada (comparação
    sem diferenciar maiúsculas/minúsculas) e processa uma por uma,
    reaproveitando processar_reserva(). Se um item falhar, os outros
    continuam."""
    cfg = carregar_config()

    print("Buscando lista completa de reservas (pode demorar um pouco)...")
    todas = listar_todas_reservas(cfg)
    print(f"Total encontrado: {len(todas)} reserva(s)")

    alvo_norm = situacao_alvo.strip().lower()
    selecionadas = [
        item for item in todas
        if (item.get("situacao") or "").strip().lower() == alvo_norm
    ]

    if not selecionadas:
        print(f"\nNenhuma reserva encontrada com situação '{situacao_alvo}'.")
        print("Situações que existem no seu CV agora:")
        for s in situacoes_disponiveis(todas):
            print(f"  - {s}")
        return

    print(f"\n{len(selecionadas)} reserva(s) com situação '{situacao_alvo}'. Processando...\n")

    ok: list[int] = []
    falhas: list[tuple[int, str]] = []
    for i, item in enumerate(selecionadas, start=1):
        idreserva = item["idreserva"]
        print(f"\n=== [{i}/{len(selecionadas)}] Reserva {idreserva} ===")
        try:
            processar_reserva(idreserva)
            ok.append(idreserva)
        except SystemExit as exc:
            print(f"      FALHOU: {exc}")
            falhas.append((idreserva, str(exc)))
        except Exception as exc:  # não deixa um erro travar o lote inteiro
            print(f"      FALHOU (erro inesperado): {exc}")
            falhas.append((idreserva, str(exc)))

    print("\n=== Resumo ===")
    print(f"Processadas com sucesso: {len(ok)}")
    print(f"Falharam: {len(falhas)}")
    for idr, motivo in falhas:
        print(f"  - {idr}: {motivo}")


# --------------------------------------------------------------------------
# Orquestração
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# A operação inteira: titular + 2º proponente + avalista + dependentes
# --------------------------------------------------------------------------

def documentos_por_grupo(dados_reserva: dict) -> dict[str, list[dict]]:
    """TODOS os grupos de documento da reserva - 'titular',
    '2_ao_promissario_comprador', 'dependente_s', avalista etc. - cada um
    deduplicado por tipo (mantendo o de maior id = mais recente), que é a
    mesma regra que documentos_do_titular_reserva() aplica ao titular.

    Deduplicar igual em todos os grupos é de propósito: mantém o resultado do
    titular comparável com o das rodadas que só olhavam ele."""
    grupos = dados_reserva.get("documentos") or {}
    resultado: dict[str, list[dict]] = {}
    for nome_grupo, docs in grupos.items():
        mais_recente_por_tipo: dict[str, dict] = {}
        for doc in docs or []:
            tipo = doc.get("tipo") or doc.get("idtipo")
            atual = mais_recente_por_tipo.get(tipo)
            id_doc = doc.get("idreservasdocumentos", 0)
            if atual is None or id_doc > atual.get("idreservasdocumentos", 0):
                mais_recente_por_tipo[tipo] = doc
        if mais_recente_por_tipo:
            resultado[nome_grupo] = list(mais_recente_por_tipo.values())
    return resultado


def _tokens(texto: str) -> tuple[set[str], set[str]]:
    """Separa um rótulo em (palavras, números), sem acento e sem caixa.

    '2_ao_promissario_comprador' -> ({'promissario','comprador'}, {'2'})
    '2º Promissário Comprador'   -> ({'promissario','comprador'}, {'2'})

    Dois detalhes que custaram uma versão errada desta função:

    1. O CV escreve o ordinal '2º' como '2_ao_' no nome do grupo, e esse
       'ao' não existe do outro lado. Por isso palavras de até 2 letras são
       descartadas, e a comparação não pode ser por prefixo.
    2. Em '2º Promissário Comprador', o NFKD transforma 'º' em 'o' e cola:
       vira o token '2o', que não é .isdigit() nem .isalpha() e sumia dos
       dois conjuntos - deixando o 2º e o 3º comprador indistinguíveis. Por
       isso os números são extraídos com findall sobre o texto inteiro, não
       dos tokens.
    """
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    numeros = set(re.findall(r"\d+", t))
    palavras = {x for x in re.split(r"[^a-z]+", t) if len(x) > 2}
    return palavras, numeros


def e_dependente(texto: str | None) -> bool:
    """O rótulo (nome do grupo de documentos ou tipo_associacao) é de dependente?

    Dependente não tem documento analisado (gestora comercial, 14/10/2026). Até aqui isso só
    valia porque a listagem de associados não traz dependente e o grupo
    'dependente_s' ficava sem par em casar_grupo_com_associado(). Um associado do
    tipo "Dependente" casaria com esse grupo pelas palavras e passaria a ser
    validado. A trava agora é explícita, pelo rótulo."""
    palavras, _ = _tokens(texto)
    return "dependente" in palavras or "dependentes" in palavras


def casar_grupo_com_associado(nome_grupo: str, associados: list[dict]) -> dict | None:
    """Qual associado corresponde a este grupo de documentos?

    O CV nomeia o grupo '2_ao_promissario_comprador' e o associado
    '2º Promissário Comprador'. Casam quando as palavras do tipo_associacao
    aparecem todas no nome do grupo E o ordinal bate - é o ordinal que separa
    o 2º do 3º comprador, e o Avalista 1 do Avalista 2.

    Devolve None quando não dá pra ter certeza - inclusive quando DOIS
    associados empatam. Isso é DE PROPÓSITO: validar os documentos de uma
    pessoa contra o cadastro de OUTRA produziria divergência de nome e de CPF
    em cada documento, que é exatamente o erro que o projeto passou semanas
    tirando. Melhor não validar do que validar errado.

    Grupo que não corresponde a associado nenhum também cai aqui. Dependente
    nem chega a esta função: quem chama pula antes, com e_dependente().
    """
    palavras_grupo, numeros_grupo = _tokens(nome_grupo)
    candidatos: list[tuple[int, dict]] = []
    for a in associados:
        palavras_tipo, numeros_tipo = _tokens(a.get("tipo_associacao"))
        if not palavras_tipo or not palavras_tipo <= palavras_grupo:
            continue
        # O ordinal tem que bater quando os dois lados o declaram - senão o
        # grupo do 2º comprador casaria com o cadastro do 3º.
        if numeros_tipo and numeros_grupo and numeros_tipo != numeros_grupo:
            continue
        nota = len(palavras_tipo) + (1 if numeros_tipo & numeros_grupo else 0)
        candidatos.append((nota, a))

    if not candidatos:
        return None
    melhor_nota = max(n for n, _ in candidatos)
    empatados = [a for n, a in candidatos if n == melhor_nota]
    if len(empatados) > 1:
        # Ex: grupo 'promissario_comprador' sem ordinal, com um 2º e um 3º
        # na operação. Qualquer escolha aqui seria chute.
        return None
    return empatados[0]


def processar_reserva_completa(
    idreserva: int, devolver_associados: bool = False,
) -> list[Path] | tuple[list[Path], list[dict], str | None]:
    """Baixa e prepara a documentação de TODAS as pessoas da reserva, uma
    pasta por pessoa, cada uma com o SEU cadastro.

    Até 05/10/2026 só o titular era processado - e a varredura de 28/09
    mediu que 47% das reservas têm 2º proponente, ou seja, quase metade das
    vendas era auditada pela metade.

    Cada pessoa ganha a sua pasta porque é assim que o resto do projeto já
    funciona: uma pasta = um cadastro = um Excel, e o analisar_documentos.py
    compara nome/CPF/endereço do documento contra o cadastro daquela pasta.
    Juntar todo mundo numa pasta só exigiria reescrever essa comparação
    inteira; separando, ela passa a valer para cada pessoa de graça.

    O nome da pasta TERMINA em '-reserva-{id}' também para o 2º proponente,
    porque extrair_idprecadastro_da_pasta() e numero_da_pasta_e_idreserva()
    dependem desse formato pra achar a modalidade da venda.

    Devolve a lista de pastas, titular primeiro.

    Com devolver_associados=True devolve (pastas, associados, falha): a lista
    de associados já consultada e, se a consulta falhou, o motivo curto (senão
    None). Nesse modo a consulta acontece mesmo sem grupo de documento além do
    titular, para quem chama enxergar associado cadastrado SEM documentos.

    09/10/2026, verificação offline do projeto. O auditar_reserva.py buscava os
    associados DE NOVO depois desta função - o dobro de chamadas numa listagem
    que já bate no limite de 429 - e, quando a busca falhava, a falha só
    aparecia no terminal: a planilha dizia "Nenhum associado ativo - só o
    titular", que é exatamente o contrário do que aconteceu. Devolvendo o que
    foi consultado aqui, quem chama usa a mesma resposta e sabe se ela falhou.
    O retorno padrão não muda, para não quebrar quem só quer as pastas
    (testar_amostra.py, processar_reserva()).
    """
    cfg = carregar_config()

    print(f"=== Reserva {idreserva} ===")
    print("[1/5] Buscando documentos da reserva...")
    dados_reserva = buscar_documentos_reserva(cfg, idreserva)
    idpessoa = dados_reserva.get("idpessoa_cv")
    if not idpessoa:
        raise SystemExit(
            "Não veio 'idpessoa_cv' na resposta de documentos - "
            "confira a resposta bruta no Postman antes de seguir."
        )
    print(f"      idpessoa_cv do titular: {idpessoa}")

    grupos = documentos_por_grupo(dados_reserva)
    outros = [g for g in grupos if g != "titular"]
    print(f"      grupos de documento: {', '.join(grupos) or '(nenhum)'}")

    print("[2/5] Buscando dados cadastrais do titular...")
    titular = buscar_dados_cliente(cfg, idpessoa)
    nome_titular = titular.get("nome", "SEM_NOME")
    print(f"      Titular: {nome_titular}")

    associados: list[dict] = []
    falha_associados: str | None = None
    if outros or devolver_associados:
        print("[3/5] Buscando as outras pessoas da operação...")
        try:
            associados = buscar_associados_da_reserva(cfg, idreserva)
            for a in associados:
                print(f"      {a.get('tipo_associacao')}: {a.get('nome')}")
        except SystemExit as e:
            # Só o começo da mensagem ("Erro 429 ao listar associados (página
            # 3)"): o resto é o corpo cru da resposta do CV, que não deve ir
            # para a planilha.
            falha_associados = str(e).split(":", 1)[0][:150]
            print(f"      FALHA ao listar os associados ({falha_associados})")
    else:
        print("[3/5] Só o titular nesta operação.")

    pastas: list[Path] = []

    def preparar(cadastro: dict, docs: list[dict], papel: str, sufixo: str) -> None:
        nome = cadastro.get("nome", "SEM_NOME")
        base = sanitizar_nome(nome)
        nome_pasta = f"{base}{sufixo}-reserva-{idreserva}"
        pasta = PASTA_SAIDAS_RESERVA / nome_pasta
        print(f"      [{papel}] {len(docs)} documento(s) -> {pasta.name}")
        docs = baixar_documentos(cfg, docs, pasta)

        # Campos sintéticos gravados na aba 'Dados Cadastrais'. É assim que o
        # analisar_documentos.py descobre de quem é a pasta sem precisar de
        # mais nenhuma chamada de API - e é o que permite montar "as outras
        # pessoas da operação" do ponto de vista de cada um.
        enriquecido = dict(cadastro)
        enriquecido["papel_na_operacao"] = papel
        enriquecido["titular_nome"] = nome_titular
        enriquecido["titular_documento"] = titular.get("documento") or ""
        gerar_excel(enriquecido, docs, pasta / f"{nome_pasta}.xlsx")
        pastas.append(pasta)

    print(f"[4/5] Baixando documentos e gerando planilhas...")
    preparar(titular, grupos.get("titular") or [], "titular", "")

    for nome_grupo in outros:
        docs = grupos[nome_grupo]
        if e_dependente(nome_grupo):
            print(f"      '{nome_grupo}': {len(docs)} documento(s) - dependente - não "
                  f"analisado (regra da gestora comercial, 14/10/2026)")
            continue
        associado = casar_grupo_com_associado(nome_grupo, associados)
        if associado and e_dependente(associado.get("tipo_associacao")):
            print(f"      '{nome_grupo}' ({associado.get('tipo_associacao')}): {len(docs)} "
                  f"documento(s) - dependente - não analisado (regra da gestora comercial, 14/10/2026)")
            continue
        if not associado:
            if falha_associados:
                # A causa aqui não é ambiguidade do grupo: a lista de associados
                # nem chegou. Dizer "não consegui identificar" mandaria a pessoa
                # procurar o problema no lugar errado (09/10/2026).
                print(
                    f"      *** '{nome_grupo}': {len(docs)} documento(s) NÃO validados - "
                    f"a consulta de associados no CV falhou ({falha_associados}). "
                    f"Rode de novo. ***"
                )
                continue
            print(
                f"      *** '{nome_grupo}': {len(docs)} documento(s) NÃO validados - "
                f"não consegui identificar a qual associado este grupo pertence. "
                f"Validar contra o cadastro errado produziria divergência em tudo. ***"
            )
            continue
        id_assoc = associado.get("idpessoa")
        if not id_assoc:
            print(
                f"      *** '{nome_grupo}' ({associado.get('nome')}): sem idpessoa no "
                f"registro de associado - não dá pra buscar o cadastro. Não validado. ***"
            )
            continue
        try:
            cadastro = buscar_dados_cliente(cfg, id_assoc)
        except SystemExit as e:
            print(f"      *** '{nome_grupo}': não consegui o cadastro ({e}). Não validado. ***")
            continue
        papel = associado.get("tipo_associacao") or nome_grupo
        preparar(cadastro, docs, papel, f"-{sanitizar_nome(papel)}")

    print(f"[5/5] {len(pastas)} pasta(s) pronta(s).")
    print("\nConcluído.")
    if devolver_associados:
        return pastas, associados, falha_associados
    return pastas


def processar_reserva(idreserva: int) -> Path:
    """Compatibilidade: devolve só a pasta do TITULAR.

    Quem quiser a operação inteira (titular + 2º proponente + avalista) usa
    processar_reserva_completa(), que devolve a lista de pastas. Este wrapper
    existe pra não quebrar chamadas antigas."""
    pastas = processar_reserva_completa(idreserva)
    if not pastas:
        raise SystemExit(f"Nenhuma pasta foi gerada para a reserva {idreserva}")
    return pastas[0]


def pedir_idreserva() -> int:
    """Pergunta o id da reserva no terminal, repetindo até vir um número válido."""
    while True:
        entrada = input("Número da reserva (idReserva): ").strip()
        if entrada.isdigit():
            return int(entrada)
        print("Digite apenas números (ex: 33609). Tente de novo.")


def menu_interativo() -> None:
    print("O que você quer fazer?")
    print("  1 - Processar UMA reserva específica (informar o número)")
    print("  2 - Processar TODAS as reservas de uma situação (ex: Vendido, Aguardando...)")
    escolha = input("Escolha 1 ou 2: ").strip()

    if escolha == "2":
        situacao = input("Situação a processar (exatamente como aparece no CV): ").strip()
        if not situacao:
            print("Situação vazia, cancelando.")
            return
        processar_por_situacao(situacao)
        return

    idreserva = pedir_idreserva()
    processar_reserva(idreserva)


def main() -> None:
    # python mvp_v2_reserva.py 33609                -> processa só essa
    # python mvp_v2_reserva.py --situacao Vendido    -> processa todas com essa situação
    # python mvp_v2_reserva.py (sem nada)            -> pergunta no terminal
    if len(sys.argv) == 2 and sys.argv[1].isdigit():
        processar_reserva(int(sys.argv[1]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "--situacao":
        processar_por_situacao(" ".join(sys.argv[2:]))
    elif len(sys.argv) == 1:
        menu_interativo()
    else:
        print("Uso:")
        print("  python mvp_v2_reserva.py                 (pergunta no terminal)")
        print("  python mvp_v2_reserva.py <idReserva>")
        print("  python mvp_v2_reserva.py --situacao <nome da situação>")
        sys.exit(1)


if __name__ == "__main__":
    main()
