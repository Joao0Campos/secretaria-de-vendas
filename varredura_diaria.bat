@echo off
rem Varredura diaria da Secretaria de Vendas (Agendador de Tarefas, 7h).
rem O log do dia fica em painel_dados\logs\varredura-AAAA-MM-DD.txt.
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 varredura_diaria.py
