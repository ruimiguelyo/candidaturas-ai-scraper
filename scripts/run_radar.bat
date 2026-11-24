@echo off
cd /d "%~dp0\.."
set MODE=%1
if "%MODE%"=="" set MODE=sniper
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m estagio_radar scan --mode %MODE% --notify
) else (
    echo Ambiente virtual .venv nao encontrado. Corre uv venv e instala o projeto primeiro.
    exit /b 1
)
