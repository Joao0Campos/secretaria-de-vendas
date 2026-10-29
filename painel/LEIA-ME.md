# Painel da Secretaria de Vendas

Mostra, por reserva, os apontamentos que já estão nas planilhas de `saidas/reserva/`, guarda a tratativa de
cada um e mede o impacto da ferramenta. Pré-cadastro saiu do painel em 20/10/2026 (decisão de produto):
`saidas/pre_cadastro/` não é lida, `/reserva/pre_cadastro/...` e `/arquivo/pre_cadastro/...` dão 404, e
os apontamentos e eventos de pré-cadastro continuam no banco, fora das telas, métricas e exportações. Não chama o CV nem a IA e não altera planilha: a única
escrita é o banco `painel_dados/painel.db` (fora do git).

## Como rodar

Da pasta do projeto, com o Python do `.venv`:

```
python -m painel criar-usuario --login ana --nome "Ana Souza" --perfil gerente
python -m painel            # o mesmo que: python -m painel servir
```

A senha é pedida no terminal (nunca por argumento). Perfis: `admin`, `gerente`, `registro`. Todos veem
as reservas e a revisão; só o `admin` vê a tela **Usuários**. O painel abre em <http://127.0.0.1:8765>,
só nesta máquina (no servidor, ver "Servidor" abaixo). O primeiro admin sai do terminal; os outros usuários
podem ser criados na tela.

## Usuários (`/admin/usuarios`, só admin) e "Trocar minha senha" (20/10/2026)

- Lista login, nome, perfil, situação, último acesso, quem criou e quem alterou por último, e o histórico
  (`usuarios_eventos` no banco: criado, desativado, reativado, perfil, senha redefinida/trocada, com quem e quando).
- Criar usuário com uma senha inicial: mesma política (mínimo 8) e o mesmo hash PBKDF2 do terminal. A senha
  dada pelo admin é provisória: no primeiro login a pessoa só vê `/trocar-senha` (as APIs respondem 403)
  até trocar.
- Desativar derruba todas as sessões da pessoa; reativar devolve o acesso. Redefinir a senha também derruba
  as sessões e obriga a troca no próximo login.
- O admin não pode se desativar, tirar o próprio perfil admin nem redefinir a própria senha pela tela (para
  a dele, "Trocar minha senha").
- `/trocar-senha` vale para todos: confere a senha atual, exige a confirmação e encerra as outras sessões
  da pessoa (a aba em uso continua). Usuário criado no terminal não precisa trocar.
- Tudo por formulário com CSRF, sem JavaScript.

## Consumo da IA (`/admin/consumo-ia`, só admin, 21/10/2026)

- O PC grava cada chamada ao Gemini em `painel_dados/consumo_ia-AAAA-MM.jsonl`, um arquivo por mês, guardando 13 meses. A tela lê só o mês atual e o anterior:
  - **Campos:** origem, número da reserva, tipo do documento no CV, páginas, modelo, erro e os tokens do `usage_metadata` (entrada, saída e raciocínio).
  - **Cache:** cada leitura que veio do cache também vira uma linha, com tokens 0.
  - **Sem dado pessoal:** nada de nome, CPF, texto lido ou nome de arquivo. A lista de campos é fechada (`painel/consumo.py`), e o tipo do documento também: só os tipos conhecidos do CV, o resto vira "outro". O número da reserva é o interno, o mesmo da planilha e do painel.
  - **Escrita:** em append, com laço até a linha inteira, trava entre as threads e entre processos (`consumo_ia.lock`, espera curta).
- Falha nesse registro nunca quebra a análise.
- O arquivo vai para o servidor no envio (opcional). A tela mostra:
  - hoje, os últimos 7 dias, o mês atual e o anterior: leituras, do cache, erros e tokens;
  - o custo estimado em US$ e R$;
  - o custo médio por documento e por reserva;
  - a projeção do mês: o gasto do mês até hoje mais a média diária de 7 dias vezes os dias que faltam.
- A linha final do log da varredura traz os tokens e o custo da rodada.

## Falhas da IA e o disjuntor (21/10/2026)

- Falha de SISTEMA na leitura (sem saldo, limite, rede, configuração, resposta inválida) não é achado sobre a venda: nunca vira divergência, apontamento ou categoria. O documento fica **aguardando leitura**:
  - vai para a aba "Falhas da IA" da planilha (arquivo, tipo, classe, mensagem, quando), nunca para "Divergencias (IA)";
  - não entra no cache e é lido de novo na rodada seguinte (a falha antiga gravada como divergência também);
  - na lista e na página da reserva aparece o selo "N documento(s) aguardando leitura", para todo mundo.
- Classes (`painel/ia_falhas.py`): `limite_ou_saldo` (spending cap, crédito, cota diária ou mensal), `rate_limit` (só com o marcador explícito de 429 por minuto: 2 novas tentativas, 5 s e 20 s), `indeterminado_429` (429 sem marcador: sem nova tentativa; 3 seguidos na rodada armam o disjuntor como "indeterminado", que pode ser limite ou saldo: confira no AI Studio), `rede_timeout_5xx`, `configuracao`, `resposta_invalida` e `bloqueada` (nem chamou: disjuntor armado). "billing details" sozinho não arma. O detalhe técnico, sem a chave, vai só para o log. Arquivo que não abre ou de tipo não suportado continua divergência: é problema do documento.
- **Disjuntor:** `limite_ou_saldo` grava `painel_dados/ia_estado.json` (estado, classe, desde, motivo). Armado, a varredura, o executor e a releitura não fazem chamada nova (o disjuntor é olhado antes de cada chamada, e no máximo `paralelismo_ia` leituras ficam em voo); o cache continua valendo.
- **Falha fechada:** arquivo ausente = livre (o PC grava um "livre"); arquivo corrompido ou ilegível BLOQUEIA, aparece no log e no banner ("estado da IA ilegível, rode ia-liberar"); um disjuntor que não conseguiu ser gravado fica em memória e bloqueia até o fim do processo.
- **Desarme:** só pelo comando, no PC, depois de recarregar o saldo ou subir o limite: `python -m painel ia-liberar`. Não há chamada de teste automática: ela gastaria o que sobrou e rearmaria o disjuntor a cada rodada.
- O estado vai para o servidor pelo envio (opcional) e a cada sinal de vida do executor (`/api/executor/estado`). Só o admin vê o banner "IA sem saldo ou limite atingido desde HH:MM"; sem sinal do PC há mais de 10 min, o banner avisa que pode estar desatualizado.

## Impacto oculto (20/10/2026)

A tela Impacto (`/impacto`, `/api/impacto` e o item do menu) está oculta por decisão de produto: ninguém usa a
ferramenta ainda e os números não dizem nada. O código e os dados continuam. Para religar:
`SECRETARIA_MOSTRAR_IMPACTO=1` no ambiente, ou `MOSTRAR_IMPACTO_PADRAO = True` em `config.py`.

Testes (offline: sem rede, sem `config.json`, CV e IA simulados; blindagem em `tests/blindagem.py`):

| arquivo | cobre |
|---|---|
| `tests/test_painel.py` | leitura de `saidas/`, filtros, login, CSRF, tratativas, sincronização, revisão, usuários e troca de senha, Impacto oculto |
| `tests/test_funil.py` | `varredura_diaria.py --atualizar-funil` e a espera da varredura pela trava |
| `tests/test_reanalise.py` | fila (`pedidos_reanalise`), rotas `/api/reanalise`, executor, timeout da IA |
| `tests/test_consumo_ia.py` | consumo da IA: registro sem dado pessoal, erro e cache, falha no registro não quebra a análise, agregação e custo, preços pelo ambiente, tela só admin, envio e linha da varredura |
| `tests/test_opcao_b.py` | opção B: chave (401/404), fila sem entrega dupla, estado/"executor desligado", upload (path traversal, limite, sha), envio retomável que não derruba nada, executor remoto |
| `tests/test_telas_reanalise.py` | telas com os templates de verdade: topo do funil, "Analisar/Reanalisar agora", JS sem inline |

`python -B painel/tests/<arquivo> -v`

## Funil e reanálise (19/10/2026)

O site **não chama o CV nem a IA**: lê `painel_dados/reservas_cv.json` (o funil, gravado pelo
`varredura_diaria.py --atualizar-funil` a cada 15 min) e grava pedidos em `pedidos_reanalise`; quem
analisa é o `executor_reanalise.py`. Tudo o que vem da listagem é "segundo a listagem do CV" (o CVDW
atrasa; a reanálise confirma pela busca direta).

- Topo: "Funil do CV atualizado às HH:MM (segundo a listagem do CV)", ao lado da última varredura.
- Lista: card "No funil do CV, ainda sem análise" com **Analisar agora**; analisada com situação fora
  do funil na listagem leva "saiu do funil: agora em X".
- Reserva: **Reanalisar agora** (só reserva da listagem e no funil); o JS (`static/painel.js`, a CSP
  bloqueia script inline) consulta o pedido a cada 5 s e recarrega ao concluir.

| rota | o quê |
|---|---|
| `POST /api/reanalise` `{"idreserva": n}` | CSRF obrigatório. Põe na fila ou devolve o pedido aberto. 400: inválido, fora da listagem, fora do funil. 429: fila cheia (20) ou reanalisada há menos de 5 min |
| `GET /api/reanalise/{idreserva}` | último pedido, posição na fila e `aguardando` (varredura, funil, outra reanálise ou executor parado) |

## Revisão de regras (`/revisao`)

Lista os apontamentos cujo status ATUAL é "ignorado", por categoria, com o "por quê" (detalhe) de
quem ignorou: é a fila para corrigir regra da auditoria. Nada muda sozinho - quem ignora pode ter
interesse em destravar a venda, então cada caso é conferido por alguém antes de virar regra.
Filtros: `motivo` = `fp_regra` (padrão: falso positivo + regra não se aplica), `fp`, `regra`, `todos`;
`periodo` = `7`, `30`, `90`, `tudo` (padrão), pela data em que foi ignorado. Também em
`/revisao.xlsx` e `/api/revisao`. Sem nome de cliente: só o papel na operação.

O Excel da revisão vai para o time (inclusive agentes de IA), então nele o texto e o "por quê" saem
com o dado pessoal trocado por um marcador nas mensagens de nome, endereço e CPF/CNPJ:
`(nome: 3 palavras, 18 letras)`, `(endereço: 6 palavras)`. As mensagens de "Outros" que trazem nome
(venda PJ, co-participante pelo nome ou pelo CPF, renda com mais de uma pessoa) recebem o mesmo marcador,
por padrão de texto (`dados.MENSAGENS_COM_NOME`); o papel/relação fica visível. A página mostra o texto inteiro. O "por quê"
já é gravado com CPF/telefone mascarados.

## Variáveis de ambiente

| variável | padrão | para quê |
|---|---|---|
| `SECRETARIA_SAIDAS` | `<projeto>/saidas` | de onde ler as planilhas |
| `SECRETARIA_PAINEL_DB` | `<projeto>/painel_dados/painel.db` | banco das tratativas, usuários e sessões |
| `SECRETARIA_MINUTOS_MANUAIS_POR_RESERVA` | (vazio) | minutos de uma conferência manual; sem ela, "tempo economizado" fica vazio |
| `SECRETARIA_COOKIE_SEGURO` | (desligado) | `1` liga `Secure` nos cookies e manda `Strict-Transport-Security` (etapa B) |
| `SECRETARIA_CV_URL_RESERVA` | `CV_URL_RESERVA_PADRAO` | link "Abrir no CV" da reserva, com `{id}` no lugar do número |
| `SECRETARIA_MOSTRAR_IMPACTO` | (desligado) | `1` religa a tela Impacto |
| `SECRETARIA_EXECUTOR_CHAVE_HASH` | (vazio) | sha256 da chave do executor do PC (`python -m painel chave-executor`); liga `/api/executor/*` e `/api/sync/*`. Sem ela, essas rotas dão 404 |
| `SECRETARIA_PRECO_<MODELO>` | tabela em `painel/consumo.py` | preço de um modelo do Gemini em US$ por milhão de tokens, `"entrada/saída/data"`, com o nome em maiúsculas e `-`/`.` trocados por `_` (ex.: `SECRETARIA_PRECO_GEMINI_2_5_FLASH=0.30/2.50/21/10/2026`). A tabela traz o gemini-2.5-flash (0,30/2,50), o 2.5-flash-lite (0,10/0,40) e o 2.5-pro (1,25/10,00), com a data. O raciocínio é cobrado como saída. Modelo sem preço = custo "desconhecido" na tela, nunca um preço errado |
| `SECRETARIA_COTACAO_USD_BRL` | `5.50` | cotação do dólar, mostrada na tela |
| `SECRETARIA_BACKUP_DIARIO` | (ligado com `SECRETARIA_EXECUTOR_CHAVE_HASH`) | backup diário do `painel.db` pelo próprio serviço, depois das 2h: API de backup do SQLite + `integrity_check`, `painel_dados/backups/`, 14 dias; o PC baixa um por dia por `GET /api/executor/backup` |
| `SECRETARIA_PROXY_CONFIAVEL` | (vazio) | IPs/redes do proxy TLS (ex.: `127.0.0.1`); liga `proxy_headers`/`forwarded_allow_ips` e o limite de login passa a usar o IP real. `*` é recusado |
| `SECRETARIA_HOST` / `SECRETARIA_PORTA` | `127.0.0.1` / `8765` | onde escutar. Proxy na mesma máquina = sempre loopback. Fora do loopback só com o proxy em OUTRA máquina e `SECRETARIA_PERMITIR_REDE=1` (o log avisa para liberar a porta só para o IP do proxy); senão recusa e explica no log |

Os links do CV só valem com `https://` num subdomínio de `cvcrm.com.br` e com `{id}`; qualquer outro valor
é ignorado (o link some) e o painel avisa no terminal ao subir.

## Segurança, em uma linha cada

- Login obrigatório em tudo, menos `/login` e `/static`. Senha em PBKDF2-SHA256 (390 mil iterações).
- Sessão de 12 h em cookie HttpOnly/SameSite=Lax; no banco fica só o hash do token.
- CSRF em todo POST; no login, pelo cookie pré-login `sv_pre` (15 min).
- 5 falhas de login por usuário ou por IP em 15 min bloqueiam; o bloqueio acaba após 15 min sem tentativa errada.
- Links de documento são opacos (`/arquivo/reserva/<id>/<token>`) e o download sai como `documento.pdf`:
  o nome do arquivo do CV traz CPF. CPF/CNPJ aparece mascarado em toda a tela.
- CSP, nosniff, X-Frame-Options e `Cache-Control: no-store` nas respostas; log de acesso desligado.

## Servidor (etapa B)

No PC de operação nada muda: sem variáveis, 127.0.0.1:8765, sem proxy, sem Secure. No servidor, tudo por
variável de ambiente no serviço:

- O painel continua em `127.0.0.1:8765`; a entrada é um proxy TLS na mesma máquina. Exemplos mínimos em
  `ferramentas/servidor/`: `Caddyfile` (certificado interno) e `web.config` (IIS + ARR), e
  `servico-painel.cmd` (NSSM) para o painel como serviço do Windows.
- `SECRETARIA_COOKIE_SEGURO=1`: cookies `Secure` (inclusive ao apagar) e `Strict-Transport-Security` em toda resposta.
- `SECRETARIA_PROXY_CONFIAVEL=127.0.0.1`: o uvicorn passa a confiar no `X-Forwarded-For` SÓ quando a
  conexão vem desse endereço, e o limite de 5 tentativas conta pelo IP real do cliente (sem isso, 5 erros
  de qualquer pessoa bloqueariam o login de todo mundo). Sem a variável, o cabeçalho é ignorado.
- `SECRETARIA_HOST` fica vazio quando o proxy está na mesma máquina (o nosso caso): `0.0.0.0` abriria a
  porta 8765 na rede e permitiria contornar o proxy, e é recusado. Só com o proxy em outra máquina e
  `SECRETARIA_PERMITIR_REDE=1`, com a porta liberada no firewall só para o IP dele.
- Serviço com conta própria, nunca LocalSystem: `servico-painel.cmd` exige a conta e dá a ela leitura
  no projeto e escrita só em `saidas\` (o site grava o que o PC envia) e `painel_dados\`.
- Só o site no servidor (opção B): `SECRETARIA_EXECUTOR_CHAVE_HASH` liga as rotas que o PC usa, com a
  chave no `Authorization` (sem sessão nem CSRF): `/api/executor/estado|proximo|concluir|enfileirar`
  (a fila) e `/api/sync/manifesto|arquivo|concluir` (os arquivos; o manifesto só calcula e as exclusões ficam para o `concluir`, no fim, com caminho validado, limite de 64 MB, sha256
  conferido, gravação atômica e mtime preservado). O passo a passo está em `docs/IMPLANTACAO.md`.
- `SECRETARIA_SAIDAS` aponta para onde as planilhas ficarem no servidor; o banco precisa de backup.
