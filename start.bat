@echo off
cd /d "%~dp0"
start "" "http://localhost:8790"
.venv\Scripts\python.exe web\server.py
