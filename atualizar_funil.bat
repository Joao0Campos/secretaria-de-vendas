@echo off
rem Atualizacao do funil da Secretaria de Vendas (Agendador de Tarefas, a cada 15 min).
rem So a listagem do CV, sem analise e sem IA. Uma linha por execucao em
rem painel_dados\logs\varredura-AAAA-MM-DD.txt.
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 varredura_diaria.py --atualizar-funil
