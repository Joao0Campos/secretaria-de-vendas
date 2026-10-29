"""Textos do bloco "Como conferir este aviso" da tela da reserva.

Um verbete por categoria de apontamento (os rótulos de testar_amostra.CATEGORIAS)
e um texto genérico para "Outros" e para categoria que ainda não tem verbete.
Quem lê é a equipe da secretaria de vendas: frases curtas, sem jargão, com
exemplo quando ajuda. Nada de dado pessoal nos exemplos.

Categoria nova em testar_amostra.CATEGORIAS: acrescente o verbete aqui (um
teste confere que nenhum rótulo ficou sem texto).
"""
from __future__ import annotations

PARTES = (
    ("o_que_e", "O que é"),
    ("onde_olhar", "Onde olhar"),
    ("como_decidir", "Como decidir"),
    ("o_que_fazer", "O que fazer"),
)

GENERICO = {
    "o_que_e": "O sistema achou algo na documentação que parece estranho e quer que uma pessoa confira.",
    "onde_olhar": "Leia o texto do aviso, abra o documento citado nele e compare com o cadastro da reserva no CV.",
    "como_decidir": "Se o documento e o cadastro dizem a mesma coisa, o aviso está errado. "
                    "Se dizem coisas diferentes, o problema é real.",
    "o_que_fazer": "Problema real: corrija no CV ou peça a correção a quem cuida. "
                   "Aviso errado: diga por quê. Se ficar na dúvida, marque \"Não tenho certeza\".",
}

VERBETES = {
    "Mesmo arquivo anexado em vários tipos de documento": {
        "o_que_e": "O mesmo arquivo foi anexado em mais de um tipo de documento. "
                   "Por exemplo, a mesma foto no campo do RG e no campo do comprovante de renda.",
        "onde_olhar": "Na pasta da pessoa no CV, abra os tipos de documento citados no aviso e veja se o arquivo é o mesmo.",
        "como_decidir": "Se os arquivos são iguais, o problema é real: um deles está no lugar errado. "
                        "Pode ser um único PDF que reúne vários documentos de verdade, e aí o aviso vale só como alerta.",
        "o_que_fazer": "Peça o documento certo para o campo que está com o arquivo repetido. "
                       "Se o PDF reúne vários documentos de fato, marque \"Aviso errado\" e explique.",
    },
    "Comprovante de endereço vencido": {
        "o_que_e": "O comprovante de endereço é velho demais. A conta do mês M vale até o fim do mês M+3. "
                   "Exemplo: uma conta de janeiro vale até 30/04.",
        "onde_olhar": "No comprovante, ache o mês de referência da conta (não o vencimento). Sem referência, use a data de emissão.",
        "como_decidir": "Conte o prazo a partir do mês de referência. Se já passou do fim do mês M+3, o problema é real. "
                        "Se o sistema pegou a data errada (por exemplo, o vencimento da conta), o aviso está errado.",
        "o_que_fazer": "Problema real: peça um comprovante mais recente. "
                       "Aviso errado: marque \"A IA leu o documento errado\" e diga qual data estava certa.",
    },
    "Comprovante de endereço sem data legível": {
        "o_que_e": "O sistema não achou nenhuma data no comprovante de endereço e não consegue saber se ele está dentro do prazo.",
        "onde_olhar": "Abra o comprovante e procure o mês de referência, a data de emissão ou a data de leitura da conta.",
        "como_decidir": "Se há uma data clara no documento, o aviso está errado: o sistema só não conseguiu ler. "
                        "Se não há data nenhuma, o problema é real.",
        "o_que_fazer": "Problema real: peça outro comprovante, com a data visível. "
                       "Aviso errado: informe a data que você viu no documento.",
    },
    "Data do comprovante em formato não reconhecido": {
        "o_que_e": "O sistema leu uma data no comprovante, mas ela veio escrita de um jeito que ele não entende. "
                   "Exemplo: \"mês 13\" ou letras no meio do número.",
        "onde_olhar": "Compare a data que aparece no aviso com a que está impressa no comprovante.",
        "como_decidir": "Se a data impressa é normal e o sistema leu torto, o aviso está errado. "
                        "Se a própria data do documento é estranha ou está rasurada, o problema é real.",
        "o_que_fazer": "Problema real: peça um comprovante legível. "
                       "Aviso errado: marque \"A IA leu o documento errado\" e escreva a data correta.",
    },
    "Endereço não bate com o cadastro": {
        "o_que_e": "O endereço do comprovante é bem diferente do endereço cadastrado no CV.",
        "onde_olhar": "Compare, lado a lado, rua, número, bairro e cidade do comprovante e do cadastro.",
        "como_decidir": "Se for só abreviação (\"Av.\" no lugar de \"Avenida\") ou complemento a mais, o aviso está errado. "
                        "Se a rua, o número ou a cidade mudam, o problema é real. "
                        "Comprovante no nome de outra pessoa da família não muda o endereço.",
        "o_que_fazer": "Problema real: corrija o cadastro no CV ou peça um comprovante do endereço certo. "
                       "Aviso errado: explique a diferença (ex.: \"só abreviação\").",
    },
    "Endereço não identificado no comprovante": {
        "o_que_e": "O sistema não conseguiu encontrar o endereço dentro do comprovante.",
        "onde_olhar": "Abra o comprovante e veja se o endereço aparece inteiro e legível.",
        "como_decidir": "Se o endereço está claro no documento, o aviso está errado. "
                        "Se está cortado, borrado ou de outra pessoa sem endereço, o problema é real.",
        "o_que_fazer": "Problema real: peça um comprovante inteiro e legível. "
                       "Aviso errado: marque \"A IA leu o documento errado\".",
    },
    "Documento obrigatório não anexado": {
        "o_que_e": "Falta na pasta um documento que a modalidade exige. Exemplo: certidão de casamento, ou comprovante de renda.",
        "onde_olhar": "Na pasta da pessoa no CV, procure o documento citado. Veja também o campo ao lado: "
                      "às vezes ele foi anexado com outro nome ou no campo errado.",
        "como_decidir": "Se o documento não está em lugar nenhum, o problema é real. "
                        "Se ele está na pasta, só em outro campo, ou se a regra não vale para esta pessoa, o aviso está errado.",
        "o_que_fazer": "Problema real: peça o documento. "
                       "Aviso errado: marque \"A regra não vale para este caso\" e diga onde está o documento ou por que não é exigido.",
    },
    "Documento em branco ou ilegível": {
        "o_que_e": "A IA achou o documento em branco, escuro demais ou de má qualidade, e não conseguiu conferir.",
        "onde_olhar": "Abra o arquivo e veja se uma pessoa consegue ler os dados principais.",
        "como_decidir": "Se você lê tudo sem esforço, o aviso está errado. "
                        "Se a folha está em branco, cortada ou apagada, o problema é real.",
        "o_que_fazer": "Problema real: peça um novo arquivo, escaneado com boa luz. "
                       "Aviso errado: marque \"A IA leu o documento errado\".",
    },
    "Arquivo não pôde ser aberto ou lido": {
        "o_que_e": "O arquivo anexado não abriu ou veio em um formato que o sistema não lê.",
        "onde_olhar": "Tente abrir o arquivo no CV. Veja a extensão (PDF, foto) e se ele tem tamanho normal.",
        "como_decidir": "Se você consegue abrir e ler, o aviso está errado: o sistema é que falhou. "
                        "Se o arquivo está corrompido ou vazio, o problema é real.",
        "o_que_fazer": "Problema real: peça o arquivo de novo. "
                       "Aviso errado: explique que o arquivo abre normalmente. "
                       "Uma nova análise da reserva costuma resolver falha de leitura passageira.",
    },
    "Renda diferente da cadastrada": {
        "o_que_e": "A renda que está no documento (Tela de Avaliação de Risco) é diferente da renda cadastrada no CV.",
        "onde_olhar": "Na Tela de Avaliação de Risco, veja a linha da renda da pessoa. No CV, veja a renda cadastrada.",
        "como_decidir": "Diferença de poucos reais é arredondamento. "
                        "Se os valores são realmente diferentes, ou a linha lida é de outra pessoa, confira de quem é cada renda "
                        "antes de decidir.",
        "o_que_fazer": "Problema real: corrija a renda no CV, ou peça a tela certa. "
                       "Aviso errado: explique (ex.: \"o sistema leu a renda do cônjuge\").",
    },
    "Renda não conferida por proponente": {
        "o_que_e": "O sistema não conseguiu comparar a renda desta pessoa. Pode ser que a tela não mostre a renda dela, "
                   "ou que o CV não tenha esse valor.",
        "onde_olhar": "Abra a Tela de Avaliação de Risco e veja se a tabela de renda traz uma linha para esta pessoa. "
                      "Veja também se o CV tem a renda preenchida.",
        "como_decidir": "Se a renda está na tela e no CV, e os valores batem, o aviso está errado. "
                        "Se falta a tabela, a linha ou o cadastro, o problema é real: a renda não pôde ser conferida.",
        "o_que_fazer": "Problema real: peça a tela completa ou preencha a renda no CV. "
                       "Se você não sabe de quem é a renda na tela, marque \"Não tenho certeza\".",
    },
    "Certidão de casamento do cônjuge vale para os dois": {
        "o_que_e": "A certidão de casamento está na pasta do cônjuge, e não na desta pessoa. "
                   "O sistema avisa para você saber que ela vale para os dois.",
        "onde_olhar": "Na pasta do cônjuge, confira se a certidão é do casal e se está legível.",
        "como_decidir": "Se a certidão está lá e é do casal, não há problema: o aviso só informa. "
                        "Se ela não está na pasta de nenhum dos dois, o problema é real.",
        "o_que_fazer": "Se está tudo certo, marque \"Aviso errado\" com \"A regra não vale para este caso\" e escreva "
                       "\"certidão na pasta do cônjuge\". Se falta a certidão, peça o documento.",
    },
    "Documento de identificação vencido": {
        "o_que_e": "A validade do RG, da CNH ou de outro documento de identificação já passou.",
        "onde_olhar": "No documento, ache a data de validade (na CNH, perto do campo \"Validade\"). "
                      "Não confunda com a data de emissão nem com a data da 1ª habilitação.",
        "como_decidir": "Se a validade impressa é anterior a hoje, o problema é real. "
                        "Se o sistema pegou outra data, o aviso está errado. "
                        "Alguns documentos não têm validade; aí também está errado.",
        "o_que_fazer": "Problema real: peça um documento dentro da validade. "
                       "Aviso errado: marque \"A IA leu o documento errado\" e diga qual é a validade certa.",
    },
    "CPF diferente do cadastro": {
        "o_que_e": "O CPF lido no documento é diferente do CPF do cadastro no CV.",
        "onde_olhar": "Compare os números, um a um. Olhe o CPF do documento e o do cadastro.",
        "como_decidir": "Se só um dígito mudou e o documento está borrado, provavelmente a IA errou na leitura. "
                        "Se o documento está nítido e o número é outro, o problema é real "
                        "(pode ser o documento de outra pessoa ou um erro de digitação no cadastro).",
        "o_que_fazer": "Problema real: corrija o CPF no CV ou peça o documento certo. "
                       "Aviso errado: marque \"A IA leu o documento errado\" e diga qual número está certo.",
    },
    "Número da CNH lido como CPF": {
        "o_que_e": "Na CNH, o sistema pegou outro número (como o número de registro ou o RG) no lugar do CPF.",
        "onde_olhar": "Na CNH, procure o campo \"CPF\" e compare com o número que o aviso mostra.",
        "como_decidir": "Se o número do aviso é o de registro, ou não é o CPF impresso, o aviso está errado. "
                        "Se o CPF da CNH é mesmo diferente do cadastro, o problema é real.",
        "o_que_fazer": "Aviso errado: marque \"A IA leu o documento errado\" e diga qual é o CPF da CNH. "
                       "Problema real: corrija o cadastro ou peça o documento certo.",
    },
    "CNPJ diferente do cadastro": {
        "o_que_e": "O CNPJ lido no documento é diferente do CNPJ cadastrado.",
        "onde_olhar": "Compare os 14 números do documento com os do cadastro.",
        "como_decidir": "Se o documento está nítido e o número é outro, o problema é real. "
                        "Se o sistema leu o número de outro campo (por exemplo, um CPF), o aviso está errado.",
        "o_que_fazer": "Problema real: corrija o CNPJ no CV ou peça o documento da empresa certa. "
                       "Aviso errado: marque \"A IA leu o documento errado\".",
    },
    "Nome diferente do cadastro": {
        "o_que_e": "O nome lido no documento é diferente do nome cadastrado no CV. O aviso mostra o quanto os dois se parecem.",
        "onde_olhar": "Compare os dois nomes, letra por letra. Veja o nome completo no documento, não só o primeiro nome.",
        "como_decidir": "Acento faltando, nome do meio abreviado ou letra trocada na leitura: aviso errado. "
                        "Sobrenome diferente, nome de outra pessoa ou nome de solteira x casada: o problema é real, "
                        "porque o cadastro precisa bater com o documento.",
        "o_que_fazer": "Problema real: corrija o nome no CV ou peça o documento certo. "
                       "Aviso errado: marque \"A IA leu o documento errado\" e escreva a diferença que você viu.",
    },
}


def como_conferir(categoria: str | None) -> dict:
    """Verbete da categoria; categoria desconhecida (e "Outros") recebe o genérico."""
    return VERBETES.get(categoria or "", GENERICO)
