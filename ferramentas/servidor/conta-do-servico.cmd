@echo off
setlocal
REM Confere se o servico roda com a conta esperada. Sai 0 se sim, 1 se nao.
REM   conta-do-servico.cmd SERVICO CONTA [ARQUIVO]
REM
REM Independe do idioma do Windows (21/10/2026, servidor interno em portugues):
REM o rotulo SERVICE_START_NAME do "sc.exe qc" sai TRADUZIDO (ex.: NOME_DE_INICIO_DO_SERVICO),
REM entao a conferencia procura o NOME da conta na saida inteira, com o PowerShell
REM (Select-String -SimpleMatch: sem expressao regular, sem a barra invertida do findstr),
REM sem diferenciar maiusculas ("NT Service\X" = "NT SERVICE\X"). Os valores vao por
REM variavel de ambiente, para nao depender de aspas.
REM ARQUIVO: so para teste - uma saida do "sc.exe qc" ja gravada, no lugar de rodar o sc.

set SC_CONTA=%~2
if "%~3"=="" (
  sc.exe qc "%~1" > "%TEMP%\sc-qc-%~1-ultimo.txt" 2>&1
  set SC_SAIDA=%TEMP%\sc-qc-%~1-ultimo.txt
) else (
  set SC_SAIDA=%~3
)
if "%SC_CONTA%"=="" exit /b 1
powershell -NoProfile -Command "if (Select-String -LiteralPath $env:SC_SAIDA -SimpleMatch -Pattern $env:SC_CONTA -Quiet) { exit 0 } else { exit 1 }"
exit /b %ERRORLEVEL%
