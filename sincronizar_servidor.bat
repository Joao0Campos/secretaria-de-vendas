@echo off
rem Envio do PC para o servidor (opcao B), pelo proprio site: Agendador, a cada 5 min.
rem Rede de seguranca: a varredura, o funil e a reanalise ja enviam no fim de cada rodada.
rem Com SECRETARIA_MODO_ENVIO local (ou sem as variaveis do servidor) nao faz nada.
rem Log em painel_dados\logs\sync-AAAA-MM-DD.txt.
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 sincronizar_servidor.py
