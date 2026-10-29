# Manual Técnico — Secretaria de Vendas

> Versão pública e higienizada do manual técnico interno. Foram removidos dados de clientes, números
> operacionais, nomes de pessoas, endereços de rede e detalhes de credenciais. O conteúdo técnico
> (arquitetura, integrações, rotinas, segurança e operação) foi mantido.

## 1. Visão geral

A ferramenta confere todos os dias, sem intervenção, a documentação das reservas no CV CRM antes da
validação do contrato e mostra as pendências num site interno. **Ela só lê o CV**: quem corrige
qualquer coisa é sempre uma pessoa.

**O que ela faz, em uma frase por etapa:**

1. Às 7h, lista as reservas que estão nas etapas Nova Reserva, Reserva Aprovada, Análise Gerencial,
   Geração de Contrato e Contrato Aguardando Validação.
2. Baixa os documentos de cada pessoa da operação (titular, 2º proponente e avalista; dependentes
   ficam de fora, por regra da área de contratos).
3. Lê cada documento com IA (Google Gemini 2.5 Flash) e extrai nome, CPF, datas, endereço e renda.
4. Confronta a leitura com o cadastro do CV usando regras escritas em Python (cartilha da área de
   contratos e respostas da gestão comercial).
5. Grava uma planilha por pessoa e publica os apontamentos no painel web, onde a equipe registra o
   que fez com cada um.

**O que ela não faz:** não altera dados no CV, não aprova nem reprova venda, não apura renda (só
confere a renda do CV contra a Tela de Avaliação de Risco) e não cobre a etapa pós-assinatura.

## 2. Arquitetura e fluxo de dados

```text
O CV é só lido; tudo o que a ferramenta produz fica no disco local

 Agendador de Tarefas ──► varredura_diaria.py ──lista──► CV CRM (API v1)
 (7h e ao entrar           (escolhe as reservas          somente leitura
  no Windows)               do dia)                            │
                                 │                             │ documentos
                                 ▼                             │ e cadastros
                          mvp_v2_reserva.py ◄──────────────────┘
                          (baixa os documentos por pessoa)
                                 │
                                 ▼
                          analisar_documentos.py ◄──páginas──► Google Gemini
                          (IA lê, Python aplica as regras)     2.5 Flash
                                 │ grava
                                 ▼
                          [saidas/]  PDFs, planilhas, cache da IA
                                 │ lê
                                 ▼
 [painel_dados/] ◄──────  painel (FastAPI)  ◄──acessa── Navegador da equipe
 tratativas, listagem,    127.0.0.1:8765, com login     (atrás de proxy TLS no servidor)
 logs

 [ ] = dados em disco local, fora do git (contêm dados pessoais)
```

A varredura e o painel são dois processos independentes que só se encontram no disco. A varredura
busca no CV, lê com a IA, aplica as regras e grava em `saidas/`. O painel lê essas planilhas e guarda
as tratativas em `painel_dados/`, sem chamar o CV nem a IA. O módulo `mvp_v1_validador.py` é a base
comum: configuração, autenticação no CV, download e repetição automática das requisições.

A leitura da IA fica guardada em cache. Assim, uma regra nova vale para a base inteira na próxima
execução, sem pagar uma nova leitura.

**Implantação híbrida (opção B).** Na implantação real, só o site foi para um servidor Windows da
rede interna; a varredura, o funil e o executor de reanálise continuam num PC de operação. O PC envia
planilhas, documentos e a listagem **pelo próprio site** (HTTPS, autenticado por uma chave cujo hash
fica no servidor), e a fila de reanálise também passa pelo site. Passo a passo em
[`IMPLANTACAO.md`](IMPLANTACAO.md).

## 3. Mapa do código

| Item | O que é |
| --- | --- |
| `analisar_documentos.py` | Núcleo: prompt da IA, todas as regras, `aplicar_regras()`, cache da leitura |
| `mvp_v1_validador.py` | Base: config, autenticação no CV, download, `requisicao_com_retry` |
| `mvp_v2_reserva.py` | Reserva e associados; gera uma pasta por pessoa |
| `varredura_diaria.py` + `.bat` | Rotina diária das 7h, funil a cada 15 min, trava e repescagem |
| `executor_reanalise.py` (+ `.pyw`) | Atende a fila de reanálise pedida no painel |
| `sincronizar_servidor.py`, `cliente_servidor.py` | Envio do PC para o site no servidor (manifesto, sha256, retomável) |
| `auditar_reserva.py` | Auditoria de uma reserva, com sugestões de correção (só sugestão) |
| `testar_amostra.py` | N reservas de um status, com resumo por categoria |
| `varrer_arquivos_repetidos.py` | Arquivo repetido por hash, sem IA |
| `gerar_consolidado.py` | Excel único com todas as pastas |
| `painel/` + `painel_servidor.pyw` | Site (FastAPI) e o lançador sem janela |
| `painel/tests/` | Testes automatizados (offline, com CV e IA simulados) |
| `ferramentas/servidor/` | Exemplos de proxy TLS (Caddy, IIS) e serviço do Windows (NSSM) |
| `ferramentas/*.xml` | Tarefas do Agendador do Windows (funil, executor, envio) |
| `docs/api-cv/` | Anotações sobre a API do CV CRM |

## 4. Integrações externas

O sistema depende de dois serviços externos: a API do CV CRM, só para leitura, e a API do Google
Gemini, para ler os documentos. Os dois saem por HTTPS (porta 443) e não há nenhuma conexão de entrada
para a varredura.

### CV CRM

Endereço base: `https://<dominio>.cvcrm.com.br/api` (o subdomínio vem do `config.json`). A
autenticação é por **e-mail + token** nos cabeçalhos (esquema v1/CVDW).

| Endpoint (GET) | Para quê |
| --- | --- |
| `/v1/cvdw/reservas` | Listagem de reservas e situação. Completa na 1ª vez; depois incremental por `a_partir_data_referencia` |
| `/v1/comercial/reservas/{id}` | Confirma a situação atual antes de gastar IA e traz o `idprecadastro` |
| `/v1/comercial/reservas/{id}/documentos` | Lista e baixa os documentos da reserva, por grupo de pessoa |
| `/v1/cvdw/reservas/associados` | 2º proponente, avalista e dependentes de cada reserva |
| `/v1/cadastros/clientes` | Cadastro de cada pessoa (nome, CPF, endereço, estado civil) |
| `/v1/comercial/precadastro/{id}` e `/documentos` | Renda do pré-cadastro e documentos do pré-cadastro |

Comportamentos medidos que o código já trata:

- **Limite de requisições (HTTP 429):** `requisicao_com_retry()` tenta até 4 vezes com espera crescente.
- **Atraso do CVDW:** a listagem pode atrasar horas; reservas listadas como "Nova Reserva" podem já
  estar canceladas. Por isso cada reserva tem a situação confirmada pela busca direta.
- **A listagem não vem ordenada por número de reserva.** Nenhuma busca pode supor ordem.

### Google Gemini

| Item | Valor |
| --- | --- |
| Modelo | `gemini-2.5-flash` (configurável em `modelo_ia`) |
| Entrada | Até 5 páginas por documento |
| Paralelismo | 5 chamadas simultâneas (`paralelismo_ia`) |
| Timeout | `TIMEOUT_IA_MS` = 120 s; estourou, falha só aquele documento |
| Consumo | Cada chamada grava o `usage_metadata` (entrada, saída e raciocínio) em `painel_dados/consumo_ia-AAAA-MM.jsonl`, uma linha por chamada, **sem** nome, CPF, texto lido ou nome de arquivo. Acertos do cache entram com tokens 0, para medir a economia. A tela "Consumo da IA" (só admin) mostra o custo estimado e a projeção do mês |

**Falhas da IA (`painel/ia_falhas.py`).** Falha de sistema (sem saldo ou limite, 429 por minuto,
rede/timeout/5xx, configuração, resposta inválida) deixa o documento "aguardando leitura": aba
"Falhas da IA" da planilha, nunca divergência, apontamento ou categoria; fora do cache; relido na
rodada seguinte. O 429 por minuto (marcador explícito) tem 2 novas tentativas (5 s e 20 s); 429 sem
marcador não é repetido, e 3 seguidos armam o disjuntor como "indeterminado".

**Disjuntor com falha fechada.** Sem saldo ou limite de gasto arma o disjuntor
(`painel_dados/ia_estado.json`): nenhuma chamada nova na varredura, no executor e na releitura até
`python -m painel ia-liberar`; o cache continua valendo. Estado ilegível **bloqueia** até o
`ia-liberar`, e um disjuntor que não pôde ser gravado bloqueia em memória até o fim do processo. Não
há chamada de teste automática: ela gastaria o que sobrou e rearmaria o disjuntor a cada rodada. O
admin vê um banner no site; a lista e a reserva mostram o selo "N documento(s) aguardando leitura".

**A IA não é determinística:** o mesmo arquivo pode ser lido de forma diferente em rodadas
diferentes. Por isso toda regra crítica fica no Python, e não no prompt.

## 5. Rotinas

| Rotina | Comando | Quando | Usa IA? |
| --- | --- | --- | --- |
| Varredura diária | `python varredura_diaria.py` | Agendador às 7h e ao entrar no Windows | Sim (só arquivo novo: cache por hash) |
| Varredura forçada | `python varredura_diaria.py --forcar` | Manual, quando a do dia já rodou | Sim |
| Só atualizar a listagem | `python varredura_diaria.py --so-listar` | Manual | Não |
| Funil (só a listagem) | `python varredura_diaria.py --atualizar-funil` (`atualizar_funil.bat`) | A cada 15 min, das 7h às 19h, de segunda a sábado | Não |
| Executor de reanálise | `python executor_reanalise.py` (`.pyw` sem janela); `--uma-vez` atende 1 pedido e sai | Residente; olha a fila a cada 15 s | Sim (só arquivo novo) |
| Auditar uma reserva | `python auditar_reserva.py <idreserva>` | Manual | Sim |
| Amostra de um status | `python testar_amostra.py "Contrato Aguardando Validação" 10` | Manual | Sim |
| Arquivo repetido em massa | `python varrer_arquivos_repetidos.py "Venda Validada" 100` | Manual | Não |
| Excel consolidado | `python gerar_consolidado.py` | Manual | Não |

**Regras da varredura diária:**

- **Uma rodada bem-sucedida por dia.** Se a de hoje terminou, os próximos disparos saem sem fazer
  nada. O marcador é `painel_dados/ultima_varredura_ok.txt`.
- **Repescagem:** reservas que falharam são tentadas de novo nos disparos seguintes do mesmo dia, até
  3 vezes por reserva. Uma reserva que falha não derruba a rodada (`ok_com_falhas`).
- **Trava:** `painel_dados/varredura.trava` impede duas rodadas ao mesmo tempo, e é a MESMA para
  varredura, repescagem, funil e executor, com o `modo` de quem a segura. A varredura espera até
  15 min pelos modos curtos; o funil com a trava ocupada sai calado; o executor deixa o pedido na fila.
  Quando não dá para provar que o dono está vivo (PID), ela expira em 3 horas.

**Cache persistente da leitura da IA.** A leitura de cada arquivo fica em
`saidas/cache_ia/<2 primeiros>/<sha256 do conteúdo>.json`. Mesmo conteúdo = nunca relido, mesmo com a
planilha recriada, o arquivo baixado de novo, com outro nome ou em outra pasta. Releitura só
explícita (`--forcar` ou `--reler-telas`). Só entra leitura válida e completa (JSON objeto, campos
mínimos do prompt, tipo identificado, resposta não cortada). Gravação atômica (`.tmp` + `os.replace`).
`--revisar-cache` apaga o que não passa nessas regras.

## 6. Painel web

Site FastAPI que lê as planilhas de `saidas/reserva/` e guarda as tratativas num banco SQLite
(`painel_dados/painel.db`). Não chama o CV nem a IA. Escuta em `127.0.0.1:8765`; no servidor, fica
atrás de um proxy TLS (detalhes em [`painel/LEIA-ME.md`](../painel/LEIA-ME.md)).

| Tela ou rota | O que mostra |
| --- | --- |
| `/` | Reservas da última varredura (seletor: última, anteriores, todas) e a data dos dados |
| `/reserva/reserva/{id}` | Uma aba por pessoa: documentos, o que a IA leu, apontamentos e botões de tratativa |
| `/admin/usuarios` | Só admin: cria, desativa/reativa, muda perfil e redefine a senha dos usuários |
| `/admin/consumo-ia` | Só admin: consumo da IA, custo estimado em US$ e R$, custo médio por documento e por reserva, projeção do mês |
| `/trocar-senha` | Trocar a própria senha (obrigatória depois de uma senha dada pelo admin) |
| `/revisao` e `/revisao.xlsx` | Apontamentos ignorados, por categoria, com o motivo: a fila para corrigir regra |
| `/exportar.xlsx` | Exportação dos apontamentos |
| `POST /api/reanalise` | Põe uma reserva na fila de reanálise (CSRF). Sem duplicado, teto de 20, só reserva no funil, 5 min entre reanálises |
| `/arquivo/reserva/{id}/{token}` | Download do documento por link opaco, sempre como `documento.pdf` |

**Tratativas.** Cada apontamento tem um status: **Aberto**; **Corrigido** (marcado por uma pessoa);
**Ignorado** (exige motivo: falso positivo da IA, regra não se aplica, corrigido fora do CV ou texto
livre); **Resolvido na reanálise** (gravado pelo sistema quando o problema some na varredura seguinte;
se um apontamento corrigido reaparece, volta a "Aberto").

**Acesso e segurança:**

- Login obrigatório em tudo, menos `/login` e `/static`. Senha em PBKDF2-SHA256 com 390 mil iterações.
- Sessão de 12 horas em cookie HttpOnly e SameSite=Lax. No banco fica só o hash do token.
- Proteção CSRF em todo POST.
- Bloqueio de 15 minutos após 5 falhas de login por usuário ou por IP (IP real via proxy confiável).
- CPF e CNPJ mascarados em toda tela. CSP, `nosniff`, `X-Frame-Options` e `Cache-Control: no-store`
  nas respostas. Log de acesso desligado.
- Perfis `admin`, `gerente` e `registro`. Toda criação e alteração de usuário fica registrada com
  autor e data (`usuarios_eventos`). Não há exclusão de usuário, para não perder a autoria das tratativas.

## 7. Regras de negócio implementadas

Toda regra que pode travar uma venda está escrita em Python e tem uma fonte registrada nos comentários
do código (cartilha da área de contratos, gestão comercial ou decisão técnica medida).

| Regra | Fonte |
| --- | --- |
| Identificação é documento com foto (RG, CNH, cartão do CPF, carteira funcional, passaporte, CRNM). Comprovante de situação cadastral do CPF sozinho não vale | Decisão do projeto |
| Identificação vencida trava a validação | Decisão do projeto |
| Comprovante de endereço do mês M vale até o último dia de M+3 | Gestão comercial |
| Certidão de estado civil não vence. Batismo, crisma e 1ª comunhão não valem; união estável sozinha não vale | Gestão comercial |
| Casados entre si: a certidão de casamento do cônjuge satisfaz a exigência | Gestão comercial |
| 2º proponente e avalista: mesmos documentos do titular; dependentes não são analisados | Gestão comercial |
| Renda conferida por proponente contra a Tela de Avaliação de Risco | Gestão comercial |
| Tolerância de renda: o maior entre R$ 50 e 1% | Decisão técnica |
| Documento não exigido pela modalidade é analisado, mas só gera aviso, nunca divergência | Medição: julgar tudo que está anexado fazia quase toda venda ter apontamento |
| Venda PJ: documentos pessoais são do representante; nunca comparar CPF dele com CNPJ | Cartilha |
| Mesmo arquivo em vários tipos de documento: comparado por hash dentro da pasta de cada pessoa, nunca entre pessoas | Medição: casais compartilham documentos |
| Cada modalidade (financiamento, venda direta, loteamento) tem sua lista de documentos exigidos | Cartilha da área de contratos |

**Divergência × aviso.** Divergência é o que trava a venda pela regra. Aviso é informação para quem
confere: leitura duvidosa da IA, documento não exigido ou campo errado.

## 8. Segurança, credenciais e LGPD

O sistema guarda cópias de documentos de identidade, comprovantes e dados cadastrais de clientes.
**Nada disso está no git**, e o `.gitignore` exclui `config.json`, `saidas/`, `painel_dados/`, PDF e DOCX.

| Segredo | Onde fica |
| --- | --- |
| E-mail + token do CV | `config.json` ou variáveis de ambiente (`CVCRM_DOMINIO`, `CVCRM_EMAIL`, `CVCRM_TOKEN`), fora do git |
| Chave da API do Gemini | `config.json` (`gemini_api_key`) ou variável `GEMINI_API_KEY`, fora do git |
| Segredo de sessão do painel | `painel_dados/painel.db`, gerado pelo próprio painel |
| Senhas dos usuários do painel | `painel.db`, só o hash PBKDF2 |
| Chave do executor (PC → site) | Variável de usuário no PC; no servidor só o hash sha256 |

**Contas separadas:** o site não tem segredo nenhum do CV nem da IA: lê `saidas/` e `painel_dados/` e
grava só o `painel.db`. O `config.json` só é usado pela varredura e pelo executor.

**Minimização de dados:** a listagem local do CV (`reservas_cv.json`) guarda só número, situação e
modalidade, **sem** nome nem CPF, por desenho. A exportação da revisão de regras troca nome, endereço
e CPF por marcadores. O registro de consumo da IA tem lista fechada de campos, sem dado pessoal.

**Regra do projeto:** o fluxo automático só lê o CV. Nos testes, `painel/tests/blindagem.py` é
carregado antes de qualquer import e bloqueia rede (socket, requests), o cliente Gemini, a leitura de
`config.json`, `saidas/` e de PDF/DOCX, qualquer escrita dentro da pasta do projeto e a importação do
script de escrita no CV.

## 9. Requisitos para hospedagem

| Item | Requisito |
| --- | --- |
| Python | 3.14 (versão em uso), com ambiente virtual próprio |
| Dependências | `requirements.txt` (versões fixas) |
| Saída de rede | HTTPS para `<dominio>.cvcrm.com.br` e `generativelanguage.googleapis.com` |
| Entrada de rede | Só HTTPS no proxy reverso. O painel continua em `127.0.0.1:8765` e nunca é exposto direto |
| Caminhos | `SECRETARIA_SAIDAS` e `SECRETARIA_PAINEL_DB` apontam para onde ficam as planilhas e o banco |

| Peça | Windows Server | Linux |
| --- | --- | --- |
| Varredura das 7h | Agendador de Tarefas com o `varredura_diaria.bat` | cron ou timer do systemd |
| Painel sempre no ar | Serviço do Windows (NSSM) chamando `python -m painel`, com conta própria (nunca LocalSystem) | Serviço do systemd |
| Executor sempre no ar | Tarefa no logon (`ferramentas/tarefa-executor.xml`) ou serviço | Serviço do systemd |
| Funil a cada 15 min | Agendador com o `atualizar_funil.bat` (`ferramentas/tarefa-funil.xml`) | timer do systemd |
| Proxy TLS | IIS com ARR ou Caddy | nginx ou Caddy |
| Trava da varredura | Confere o dono pelo PID (`OpenProcess`) | Quando não dá para provar o dono, expira em 3 h |

## 10. Operação

| Arquivo em `painel_dados/` | O que diz |
| --- | --- |
| `ultima_varredura_ok.txt` | Hora em que a rodada do dia terminou e as reservas pendentes, com o erro de cada uma |
| `varreduras.jsonl` | Uma linha por rodada: reservas no funil, analisadas, falhas, duração |
| `logs/varredura-AAAA-MM-DD.txt` | Log completo do dia |
| `logs/painel.txt` | Partidas do site e erros do servidor |

**Como reprocessar:**

- **Uma reserva:** `python auditar_reserva.py <idreserva>` (ou "Reanalisar agora" no painel).
- **Uma regra mudou:** basta rodar de novo. As regras são reaplicadas sobre a leitura em cache, sem custo de IA.
- **Forçar a IA a reler:** `--forcar`. Usar só quando a leitura está errada, nunca para testar regra.
- **IA sem saldo ou limite atingido:** recarregue o saldo ou suba o limite e rode `python -m painel ia-liberar`.
- **Trava presa:** é recuperada sozinha quando o dono está provadamente morto, ou em até 3 horas.

## 11. Como mudar uma regra com segurança

1. Confirme a fonte da regra (cartilha, área de contratos ou decisão registrada). Sem fonte, ela não vai para o código.
2. Altere a regra em Python, em `analisar_documentos.py`. O prompt da IA só reforça; ele não garante nada.
3. Meça antes de entregar: rode as regras sobre as planilhas já processadas e conte quantos
   apontamentos mudam por categoria. Se a regra dispara em mais de ~10% da base, quase sempre ela está errada.
4. Não use `--forcar` para testar regra. A releitura da IA muda junto e mistura as duas coisas.
5. Mensagem nova precisa de categoria (`CATEGORIAS` em `testar_amostra.py`). Renomear categoria exige
   o alias em `CATEGORIAS_RENOMEADAS` (`painel/banco.py`), senão as tratativas antigas se soltam.

**Armadilhas conhecidas:**

- **Exigido ≠ anexado.** A cartilha define o que é exigido. Julgar tudo que está anexado fazia quase
  toda venda dar apontamento.
- **Documento da operação × documento da pessoa.** A Tela de Risco é uma por operação e pode estar na
  pasta de qualquer pessoa. Identidade e comprovante são de cada pessoa.
- **Arquivo repetido é comparado dentro da pasta de cada pessoa**, nunca entre pessoas.
- **Na pasta de reserva, o número no nome é o idReserva**, nunca o idPré-cadastro. Confundir os dois
  já trouxe a renda de outra pessoa, em silêncio.
- **Na dúvida sobre a qual pessoa pertence um grupo de documentos, não valide.**
