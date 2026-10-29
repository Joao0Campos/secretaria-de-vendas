# Implantação: só o site no servidor (opção B)

> Versão pública: endereços, contas e caminhos da rede interna foram trocados por placeholders
> (`SERVIDOR_INTERNO`, `C:\SecretariaVendas`, `DOMINIO\usuario`).

O **site** vai para um servidor Windows da rede interna (`SERVIDOR_INTERNO` nesta cópia pública). A varredura, o funil e o executor continuam num PC de operação.

- O PC envia as planilhas, os documentos e a listagem **pelo próprio site** (HTTPS, com a chave do executor).
- A fila de reanálise também passa pelo site.
- Depois da virada, o `painel.db` fica **só no servidor**, e só o site grava nele.
- A porta 445 (compartilhamento) não passa pela VPN, por isso não há pasta compartilhada.

**Ordem:** preparar o servidor (1) e o envio do PC (2), fazer o **ensaio** (3) com um banco de teste e, só com tudo aprovado, a **virada** (4).

**Modos.** O modo é escolhido por componente:

- `SECRETARIA_MODO_ENVIO`: o envio dos arquivos ao servidor.
- `SECRETARIA_MODO_EXECUTOR`: a fila de reanálise.

Sem nenhuma variável, o PC funciona como sempre (local). Qualquer configuração parcial faz o envio, a varredura e o executor **pararem**, com o motivo no log:

- uma variável remota definida sem o modo do componente;
- `remoto` sem a URL ou a chave;
- executor remoto com o envio local.

Eles nunca caem no `painel.db` do PC por engano.

## 1. No servidor (preparação)

1. **Python 3.14** para todos os usuários. **Caddy** e **NSSM** em `C:\Ferramentas\` (ou outra pasta, desde que esteja no PATH).
2. **Projeto.** Copie a pasta do projeto para `C:\SecretariaVendas`, **sem** `saidas\`, `painel_dados\`, `config.json` e `.venv\`. O site não usa segredo nenhum do CV nem da IA.
3. **Ambiente:**
   ```
   cd C:\SecretariaVendas
   py -3.14 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
4. **Chave do executor.** No PC de operação, rode `python -m painel chave-executor`. Ele mostra a chave, que fica no PC, e o hash, que vai para o servidor.
5. **Painel como serviço** (sobe com um `painel.db` NOVO, de ensaio). Num prompt de administrador:
   ```
   set SECRETARIA_EXECUTOR_CHAVE_HASH=<o hash do passo 4>
   ferramentas\servidor\servico-painel.cmd "NT SERVICE\SecretariaPainel"
   ```
   - O script recusa rodar sem conta (nunca usa LocalSystem).
   - A conta virtual é aplicada com `sc.exe config`: o NSSM 2.24, a versão estável do nssm.cc, não aceita `NT SERVICE\...` no ObjectName ("Invalid account name"). Uma conta local ou de domínio, com senha, continua pelo NSSM.
   - O script confere a conta procurando o próprio NOME dela na saída do `sc.exe qc` (`ferramentas\servidor\conta-do-servico.cmd`). Não procura pelo rótulo, porque no Windows em português o `SERVICE_START_NAME` sai traduzido (`NOME_DE_INICIO_DO_SERVICO`).
   - Se a conta não ficou aplicada, ele remove o serviço, para não sobrar rodando como LocalSystem.
   - Se uma tentativa anterior deixou o serviço criado, rode `nssm stop SecretariaPainel` e `nssm remove SecretariaPainel confirm` e depois o script de novo.
   - Ele dá à conta leitura no projeto e escrita em `saidas\` e `painel_dados\`.
   - Ele liga `SECRETARIA_PROXY_CONFIAVEL=127.0.0.1` e `SECRETARIA_COOKIE_SEGURO=1`, além do backup diário.
   - O painel escuta só em `127.0.0.1:8765` e sobe sozinho com o Windows.
6. **Caddy como serviço, com conta própria** (o NSSM sozinho rodaria como LocalSystem, que pode tudo na máquina). O endereço já está em `ferramentas\servidor\Caddyfile`, com o certificado da CA interna do Caddy.
   - A conta virtual `NT SERVICE\SecretariaCaddy` basta: no Windows, ouvir na 443 não exige administrador (o Caddy abre o próprio socket, sem http.sys).
   - Ela só lê o executável e o Caddyfile e só escreve na pasta de dados do Caddy (certificados). Não tem acesso a `saidas\` nem a `painel_dados\`.
   - **Confira o nome do executável antes.** O Windows esconde as extensões, e o arquivo baixado pode ter ficado `caddy.exe.exe`. Confira com `dir C:\Ferramentas`. Se estiver assim, renomeie: `ren C:\Ferramentas\caddy.exe.exe caddy.exe`.
   - A conta vai pelo `sc.exe`, não pelo NSSM: o NSSM 2.24 não aceita a conta virtual ("Invalid account name").
   ```
   mkdir C:\SecretariaVendas\caddy_dados
   nssm install SecretariaCaddy C:\Ferramentas\caddy.exe run --config C:\SecretariaVendas\ferramentas\servidor\Caddyfile
   sc.exe config SecretariaCaddy obj= "NT SERVICE\SecretariaCaddy"
   sc.exe qc SecretariaCaddy
   nssm set SecretariaCaddy Start SERVICE_AUTO_START
   nssm set SecretariaCaddy AppEnvironmentExtra XDG_DATA_HOME=C:\SecretariaVendas\caddy_dados XDG_CONFIG_HOME=C:\SecretariaVendas\caddy_dados
   icacls C:\Ferramentas\caddy.exe /grant "NT SERVICE\SecretariaCaddy:RX"
   icacls C:\SecretariaVendas\ferramentas\servidor /grant "NT SERVICE\SecretariaCaddy:(OI)(CI)R"
   icacls C:\SecretariaVendas\caddy_dados /grant "NT SERVICE\SecretariaCaddy:(OI)(CI)M"
   nssm start SecretariaCaddy
   ```
   - Antes do `nssm start`, o `sc.exe qc` tem de mostrar a conta `NT SERVICE\SecretariaCaddy` (às vezes escrita `NT Service\...`). O rótulo dela muda com o idioma: `SERVICE_START_NAME` em inglês, `NOME_DE_INICIO_DO_SERVICO` em português. Para conferir sem depender do idioma: `ferramentas\servidor\conta-do-servico.cmd SecretariaCaddy "NT SERVICE\SecretariaCaddy"` (sai 0 se a conta está certa).
   - Se o Caddy já subiu como LocalSystem, corrija com `nssm stop SecretariaCaddy`, depois `sc.exe config SecretariaCaddy obj= "NT SERVICE\SecretariaCaddy"`, depois `nssm start SecretariaCaddy`, e confira de novo com `sc.exe qc`.

   Depois de subir, copie para o PC de operação a raiz da CA interna: `C:\SecretariaVendas\caddy_dados\caddy\pki\authorities\local\root.crt`.
7. **Firewall.**
   - Libere a entrada na porta **443** (TCP), de preferência só da faixa da VPN e da rede interna.
   - A porta 8765 **não** é liberada.
8. **Admin de ensaio**, só no banco de teste (some na virada, quando o banco do PC chega):
   ```
   .venv\Scripts\python.exe -m painel criar-usuario --login ensaio --nome "Ensaio" --perfil admin
   ```

## 2. No PC de operação: só o envio em modo remoto

O painel do PC continua no ar, intocado. A varredura, o funil e o executor continuam locais, no `painel.db` do PC. Só o envio dos arquivos passa a ir para o servidor.

9. **Variáveis do usuário** (as tarefas rodam como ele). Depois, saia e entre no Windows para as tarefas e o executor lerem as variáveis.
   ```
   setx SECRETARIA_MODO_ENVIO remoto
   setx SECRETARIA_MODO_EXECUTOR local
   setx SECRETARIA_SERVIDOR_URL https://SERVIDOR_INTERNO
   setx SECRETARIA_EXECUTOR_CHAVE <a chave do passo 4>
   setx SECRETARIA_CA_BUNDLE C:\SecretariaVendas\ferramentas\servidor\caddy-root.crt
   ```
   - O último caminho aponta para o `root.crt` do passo 6, copiado para essa pasta.
   - A chave fica só na variável de usuário do operador (`setx` grava em `HKCU\Environment`, que só ele e os administradores leem). Nunca vai em variável de sistema, `.bat`, script ou no git.
10. **Tarefa do envio a cada 5 min** (rede de segurança: a varredura e o funil também enviam no fim de cada rodada):
    ```
    schtasks /Create /TN "Secretaria de Vendas\Envio ao servidor" /XML ferramentas\tarefa-sync.xml
    ```

## 3. Ensaio (antes da virada)

Com o servidor no banco de teste e o PC só enviando, cumpra o checklist abaixo. Os comandos do PC rodam na pasta do projeto; os do servidor, em `C:\SecretariaVendas`, num prompt de administrador. Anote o resultado de cada item.

- [ ] **E1. Primeira carga completa** (cerca de 870 MB; pode ser interrompida e retomada à vontade). No PC:
  ```
  .venv\Scripts\python.exe sincronizar_servidor.py --completo
  ```
  - Repita até o log `painel_dados\logs\sync-AAAA-MM-DD.txt` trazer "0 falha(s)".
  - No site de ensaio (`https://SERVIDOR_INTERNO`, login `ensaio`), a lista de reservas é a mesma do PC.
- [ ] **E2. O servidor bate com o PC** (contagem e sha256 de cada arquivo; só compara, não envia nem apaga). No PC:
  ```
  .venv\Scripts\python.exe sincronizar_servidor.py --comparar
  ```
  Tem de terminar com "BATEM". Se não bater, ele lista as diferenças: o que falta no servidor, o que está diferente, o que só existe no servidor e o que o PC não conseguiu ler.
- [ ] **E3. Um pedido de teste por UM executor remoto**, contra o banco de teste.
  1. No site de ensaio, abra uma reserva e clique **Reanalisar agora**.
  2. No PC, abra um prompt e rode o executor remoto uma vez:
     ```
     set SECRETARIA_MODO_EXECUTOR=remoto
     .venv\Scripts\python.exe executor_reanalise.py --uma-vez
     ```
     O `set` vale só para esse prompt. O executor residente segue local, no ar: os dois usam instâncias separadas e a mesma trava do PC.
  3. **Confira:**
     - No site de ensaio, o pedido chega a "Concluído" e a análise é a nova.
     - No `painel.db` do PC, nada mudou: o painel do PC continua mostrando o mesmo de antes.
- [ ] **E4. Restauração real de um backup.** No servidor:
  1. Pare o site e gere um backup: `nssm stop SecretariaPainel` e `.venv\Scripts\python.exe -m painel copiar-banco --para painel_dados\ensaio-bom.db`.
  2. Quebre uma cópia de propósito:
     ```
     copy painel_dados\ensaio-bom.db painel_dados\ensaio-quebrado.db
     .venv\Scripts\python.exe -c "f=open('painel_dados/ensaio-quebrado.db','r+b'); f.seek(0); f.write(b'X'*4096); f.close()"
     .venv\Scripts\python.exe -m painel conferir-banco --arquivo painel_dados\ensaio-quebrado.db
     ```
     O último comando tem de acusar CORROMPIDO.
  3. Restaure seguindo a seção 6, usando `ensaio-bom.db` como o backup. O `conferir-banco` tem de mostrar `integrity_check ok`.
  4. Suba o site e entre com `ensaio`.
  5. No dia seguinte, confira o backup automático:
     - existe `painel_dados\backups\painel-AAAA-MM-DD.db` das 2h;
     - o selo "Backup" no topo do site (só o admin vê) está verde;
     - o PC baixou o backup para `painel_dados\backups_servidor\`.
- [ ] **E5. Quedas.**
  - **Reiniciar o servidor** (`shutdown /r /t 0`): depois do boot, o site abre sem ninguém mexer (os dois serviços voltam sozinhos).
  - **Parar o Caddy** (`nssm stop SecretariaCaddy`):
    - o site fica fora;
    - no PC, o `sync-*.txt` registra "servidor indisponível" e a varredura, o funil e o executor locais seguem normais.
    - Suba de novo (`nssm start SecretariaCaddy`): o próximo envio, em até 5 min, completa.
  - **PC sem VPN.** Desconecte, espere uma rodada (o log registra a falha), reconecte e espere a próxima. O envio retoma sozinho, e o E2 volta a dar "BATEM".
- [ ] **E6. Restaurar o backup BAIXADO no PC** (a cópia fora do servidor, não só a de lá).
  1. No PC, confira a cópia: `.venv\Scripts\python.exe -m painel conferir-banco --arquivo painel_dados\backups_servidor\painel-AAAA-MM-DD.db` tem de mostrar `integrity_check ok`.
  2. Leve esse arquivo para o servidor pelo RDP e restaure seguindo a seção 6, usando-o como o backup.
  3. Confira: `conferir-banco` mostra `ok`, o site sobe e o login `ensaio` entra.
- [ ] **E7. Falha de envio depois do manifesto** (nada pode sumir, e a listagem não pode ser publicada).
  1. No PC, faça um arquivo e a listagem precisarem subir: `.venv\Scripts\python.exe -c "import os,glob; [os.utime(p) for p in glob.glob('saidas/reserva/*-reserva-<N>/*.xlsx') + ['painel_dados/reservas_cv.json']]"` (troque `<N>` por uma reserva qualquer).
  2. Rode o envio fingindo a falha desses arquivos: `.venv\Scripts\python.exe sincronizar_servidor.py --ensaio-falhar-em reserva-<N>`. Ele tem de dizer "a listagem NÃO foi publicada".
  3. Confira:
     - o `--comparar` mostra só a planilha e a listagem como "diferentes", e "só no servidor: 0";
     - o log traz "publicação adiada" e, se algo tiver sumido do PC, "exclusões adiadas".
  4. Rode o envio normal (`sincronizar_servidor.py`) e o `--comparar` volta a dar "BATEM".

**Critério de aprovação:** todos os itens E1 a E7 OK, o `--comparar` dando "BATEM" no fim e nenhum "FALHOU" em `painel_dados\logs\backup.txt`. Só então a virada.

## 4. Virada

1. **Congele TODOS os escritores do PC.** São o site (tratativas), o executor (fila), o funil e a varredura. No PC, na pasta do projeto:
   ```
   .venv\Scripts\python.exe -m painel congelar-pc
   ```
   - Ele desabilita as tarefas Painel, Executor, Funil e Varredura diaria.
   - Ele confere que não há trava nem pedido em andamento. Se houver, não mata nada: diz para esperar e rodar de novo.
   - Ele encerra o site e o executor do PC e confere que nada do projeto ficou rodando.
   - Só siga quando ele disser **"PC congelado"**. Com qualquer escritor no ar, alguém poderia gravar no banco do PC depois da cópia, e haveria dois bancos.
2. **Leve o `painel.db` do PC para o servidor** (usuários, tratativas e fila). Ele substitui o banco de ensaio. No servidor:
   1. Pare o site: `nssm stop SecretariaPainel`.
   2. Guarde o banco de ensaio: `.venv\Scripts\python.exe -m painel copiar-banco --para painel_dados\painel-ensaio.db`.
   3. Grave o `painel.db` do PC (área de transferência ou unidade redirecionada do RDP) como `painel_dados\painel.db`.
   4. Confira: `.venv\Scripts\python.exe -m painel conferir-banco` tem de mostrar `integrity_check ok`.
   5. Suba o site: `nssm start SecretariaPainel`.
3. **Executor remoto e religar o PC.** No PC:
   ```
   setx SECRETARIA_MODO_EXECUTOR remoto
   .venv\Scripts\python.exe -m painel descongelar-pc
   ```
   - O `descongelar-pc` habilita Executor, Funil e Varredura diaria. O Painel do PC continua desabilitado (`--com-painel` só para desfazer a virada).
   - Depois, saia e entre no Windows: o executor sobe já remoto.
4. **Confira:**
   - Um "Reanalisar agora" de verdade chega a "Concluído".
   - O site não mostra "executor desligado".
   - O `--comparar` dá "BATEM".

   O `painel.db` do PC fica parado, só como lembrança. A partir daqui, o banco existe só no servidor.

## 5. No dia a dia

- O site mostra "Dados de hoje" depois da varredura.
- **Pedidos de reanálise:**
  - Um pedido só conclui depois de a planilha nova chegar ao servidor. Se o envio falhar, o pedido fica "falhou" com o motivo, e a planilha sobe na próxima sincronização.
  - Sem sinal do executor por mais de 1 min, o site mostra "executor desligado" para os pedidos na fila.
- **Envio:**
  - Uma falha de rede nunca derruba a varredura nem a análise: fica no log, e o próximo envio (no máximo 5 min) completa.
  - A listagem só é publicada depois de todas as planilhas da rodada.
  - A rodada de 5 min compara tamanho e data. A primeira depois das 3h confere tudo por sha256 e corrige o que divergir; para forçar: `python sincronizar_servidor.py --completo`.
  - Se o PC falhar ao ler algum arquivo ou pasta, ou achar um link ou reparse point (ex.: arquivo "só online" do OneDrive), naquela rodada nada é apagado no servidor (o log diz "manifesto incompleto").
  - As exclusões no servidor acontecem só no fim de uma rodada inteira (depois de todos os envios e da publicação).
  - Obrigatórios no PC: `saidas\reserva\`, `painel_dados\reservas_cv.json` e `painel_dados\varreduras.jsonl`. Faltando um deles, a rodada fica incompleta: nada é apagado no servidor e a listagem não é publicada.
- `SECRETARIA_SERVIDOR_URL` só aceita `https://`.

## 6. Backup do banco

O admin vê o estado do backup no topo do site: a idade do último backup bom e o espaço livre. O alerta aparece se não houver backup, se o último bom tiver mais de 36 h, se a última tentativa falhou ou se o disco tiver menos de 2 GB livres.

- **No servidor:** o próprio serviço do painel faz um backup por dia, depois das 2h (ou ao subir, se perdeu o horário).
  - Usa a API de backup do SQLite, nunca a cópia do arquivo aberto.
  - Passa pelo `PRAGMA integrity_check` antes de valer.
  - Fica em `painel_dados\backups\painel-AAAA-MM-DD.db`, guardando 14 dias.
  - Log em `painel_dados\logs\backup.txt`.
- **Fora do servidor:** a primeira sincronização depois das 3h baixa o backup mais recente para o PC de operação.
  - Vai para `painel_dados\backups_servidor\`, com sha256 e integridade conferidos, guardando 14.
  - Fica registrado no log `sync-*.txt`.

**Restauração** (no servidor, prompt de administrador, em `C:\SecretariaVendas`):

1. **Pare o site:** `nssm stop SecretariaPainel`.
2. **Guarde o banco atual:** `move painel_dados\painel.db painel_dados\painel-com-problema.db`.
3. **Ponha o backup no lugar:** `copy painel_dados\backups\painel-AAAA-MM-DD.db painel_dados\painel.db` (ou um de `backups_servidor\` do PC, levado pelo RDP).
4. **Confira a integridade:** `.venv\Scripts\python.exe -m painel conferir-banco` tem de mostrar `integrity_check ok`. Se não mostrar, volte ao passo 3 com o backup do dia anterior.
5. **Suba o site:** `nssm start SecretariaPainel` e abra `https://SERVIDOR_INTERNO`. As tratativas feitas depois do backup se perdem. As reservas não, porque vêm das planilhas que o PC envia.

## 7. Chave do executor: trocar e revogar

A chave autentica o PC no site: com ela, envia arquivos e mexe na fila. O servidor guarda só o hash (`SECRETARIA_EXECUTOR_CHAVE_HASH`); o PC guarda a chave na variável de usuário do operador (passo 9).

**Revogar** (suspeita de vazamento, PC perdido): no servidor, troque o hash por um novo (ou apague-o, o que desliga as rotas do PC) e reinicie o site. A chave velha passa a receber 401 na hora; o PC registra a recusa no log e para de enviar até receber a nova.

```
nssm set SecretariaPainel AppEnvironmentExtra SECRETARIA_PROXY_CONFIAVEL=127.0.0.1 SECRETARIA_COOKIE_SEGURO=1 SECRETARIA_EXECUTOR_CHAVE_HASH=<hash novo>
nssm restart SecretariaPainel
```

A lista do `AppEnvironmentExtra` é substituída inteira: repita as outras variáveis.

**Trocar** (rotina: a cada 6 meses, ou quando alguém com acesso ao PC sair):

1. No PC, gere a nova: `python -m painel chave-executor`.
2. No servidor, ponha o hash novo e reinicie o site (como em "Revogar").
3. No PC, grave a chave nova (`setx SECRETARIA_EXECUTOR_CHAVE <chave nova>`), saia e entre no Windows (reinicia o executor e as tarefas com a variável nova).
4. Confira: o próximo `sync-*.txt` traz "0 falha(s)" e o site deixa de mostrar "executor desligado".
