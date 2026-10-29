@echo off
setlocal
REM Painel da Secretaria de Vendas como servico do Windows, com NSSM (https://nssm.cc).
REM So o SITE vai para o servidor; varredura, funil e executor continuam no PC de operacao.
REM
REM Uso, num prompt de ADMINISTRADOR, na pasta do projeto no servidor:
REM   servico-painel.cmd CONTA [SENHA]
REM     CONTA  conta PROPRIA do servico, sem administrador. Exemplos:
REM            "NT SERVICE\SecretariaPainel"  conta virtual do proprio servico (sem senha)
REM            ".\svc_secretaria"             usuario local criado pela TI (com SENHA)
REM            "DOMINIO\svc_secretaria"        usuario de dominio (com SENHA)
REM     SENHA  obrigatoria para usuario local ou de dominio; nao se usa na conta virtual.
REM Sem CONTA o script PARA: sem ela o NSSM rodaria o painel como LocalSystem,
REM que pode tudo na maquina.
REM
REM Conta virtual: o NSSM 2.24 (a versao estavel do nssm.cc) NAO aceita "NT SERVICE\X"
REM no ObjectName ("Invalid account name" / pede usuario e senha). Ela e aplicada com
REM sc.exe config (21/10/2026, implantacao real no servidor interno); o NSSM fica so com
REM o resto. Conta local ou de dominio com senha continua pelo NSSM. Nos dois casos a
REM conta e conferida pelo NOME na saida do "sc.exe qc" (conta-do-servico.cmd, que nao
REM depende do idioma); se nao ficou aplicada, o servico e REMOVIDO,
REM para nunca sobrar instalado como LocalSystem.
REM
REM Antes, no mesmo prompt, o hash da chave do executor do PC (opcao B: o PC envia
REM os arquivos e a fila pelo site). Sem ele, as rotas do PC ficam desligadas:
REM   set SECRETARIA_EXECUTOR_CHAVE_HASH=<hash de: python -m painel chave-executor>
REM
REM Permissoes minimas (aplicadas abaixo com icacls):
REM   pasta do projeto (codigo e .venv): leitura e execucao
REM   saidas\                          : leitura e escrita (o site grava o que o PC envia)
REM   painel_dados\                    : leitura e escrita (painel.db, listagem, logs)
REM
REM Por que NSSM e nao "sc create": o sc so registra programas que ja sao servicos
REM do Windows; python.exe nao e, e o servico nao sobe. NSSM (ou WinSW) faz esse papel.

set SERVICO=SecretariaPainel
set CONTA=%~1
set SENHA=%~2
if "%CONTA%"=="" (
  echo ERRO: informe a conta do servico. Ex.: servico-painel.cmd "NT SERVICE\%SERVICO%"
  echo Sem ela o servico rodaria como LocalSystem. Nada foi feito.
  exit /b 1
)
if /i "%CONTA%"=="LocalSystem" goto conta_proibida
if /i "%CONTA%"=="NT AUTHORITY\SYSTEM" goto conta_proibida

set VIRTUAL=
if /i "%CONTA:~0,11%"=="NT SERVICE\" set VIRTUAL=1
if defined VIRTUAL if /i not "%CONTA%"=="NT SERVICE\%SERVICO%" (
  echo ERRO: a conta virtual precisa ter o nome do servico: "NT SERVICE\%SERVICO%". Nada foi feito.
  exit /b 1
)
if not defined VIRTUAL if "%SENHA%"=="" (
  echo ERRO: conta local ou de dominio precisa da SENHA: servico-painel.cmd "%CONTA%" SENHA. Nada foi feito.
  exit /b 1
)

sc.exe query %SERVICO% >nul 2>&1
if not errorlevel 1 (
  echo ERRO: o servico %SERVICO% ja existe. Remova antes: nssm stop %SERVICO% e nssm remove %SERVICO% confirm
  exit /b 1
)

set PASTA=%~dp0..\..
pushd "%PASTA%" || exit /b 1
set PASTA=%CD%
popd

nssm install %SERVICO% "%PASTA%\.venv\Scripts\python.exe" "-m painel servir" || exit /b 1
nssm set %SERVICO% AppDirectory "%PASTA%"
nssm set %SERVICO% DisplayName "Secretaria de Vendas - painel"
nssm set %SERVICO% Start SERVICE_AUTO_START

REM A conta: virtual pelo sc.exe; local/dominio pelo NSSM. Conferida antes de seguir.
if defined VIRTUAL (
  sc.exe config %SERVICO% obj= "%CONTA%" >nul || goto conta_nao_aplicada
) else (
  nssm set %SERVICO% ObjectName "%CONTA%" "%SENHA%" || goto conta_nao_aplicada
)
REM Conferencia independente do idioma (o rotulo do "sc.exe qc" sai traduzido no Windows em portugues).
call "%~dp0conta-do-servico.cmd" %SERVICO% "%CONTA%" || goto conta_nao_aplicada

REM Atras do proxy na MESMA maquina (Caddyfile ou web.config desta pasta): IP real no
REM limite de login, cookies Secure e HSTS. O painel continua em 127.0.0.1:8765
REM (SECRETARIA_HOST fica vazio: 0.0.0.0 com proxy local e recusado).
set EXTRA=SECRETARIA_PROXY_CONFIAVEL=127.0.0.1 SECRETARIA_COOKIE_SEGURO=1
if not "%SECRETARIA_EXECUTOR_CHAVE_HASH%"=="" set EXTRA=%EXTRA% SECRETARIA_EXECUTOR_CHAVE_HASH=%SECRETARIA_EXECUTOR_CHAVE_HASH%
nssm set %SERVICO% AppEnvironmentExtra %EXTRA%

REM Saida do servico (inclui o motivo, se a configuracao for recusada ao subir).
if not exist "%PASTA%\painel_dados\logs" mkdir "%PASTA%\painel_dados\logs"
nssm set %SERVICO% AppStdout "%PASTA%\painel_dados\logs\painel-servico.txt"
nssm set %SERVICO% AppStderr "%PASTA%\painel_dados\logs\painel-servico.txt"
nssm set %SERVICO% AppRotateFiles 1
nssm set %SERVICO% AppRotateBytes 10485760

REM Permissoes minimas da conta (herdadas pelas subpastas e arquivos).
icacls "%PASTA%" /grant "%CONTA%:(OI)(CI)RX" >nul || exit /b 1
if not exist "%PASTA%\saidas\reserva" mkdir "%PASTA%\saidas\reserva"
icacls "%PASTA%\saidas" /grant "%CONTA%:(OI)(CI)M" >nul || exit /b 1
icacls "%PASTA%\painel_dados" /grant "%CONTA%:(OI)(CI)M" >nul || exit /b 1

nssm start %SERVICO%
echo Servico %SERVICO% instalado com a conta %CONTA%.
echo Confira: sc.exe qc %SERVICO%   (a conta de inicio - SERVICE_START_NAME, ou NOME_DE_INICIO_DO_SERVICO
echo no Windows em portugues - tem de ser %CONTA%)
echo Depois: nssm status / stop / restart %SERVICO%   e   nssm remove %SERVICO% confirm
echo O primeiro admin do painel sai do terminal, na pasta do projeto:
echo   .venv\Scripts\python.exe -m painel criar-usuario --login admin --nome "Administrador" --perfil admin
exit /b 0

:conta_nao_aplicada
echo ERRO: a conta %CONTA% nao foi aplicada ao servico. Removendo %SERVICO% para ele nao ficar como LocalSystem.
nssm remove %SERVICO% confirm >nul 2>&1
exit /b 1

:conta_proibida
echo ERRO: %CONTA% e a conta do sistema, que pode tudo na maquina. Use uma conta propria.
exit /b 1
