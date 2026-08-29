@echo off
setlocal
cd /d "%~dp0"
echo =========================================================
echo  Agregador Diario de Vagas IA - Junior / Trainee / Estagio
echo =========================================================
set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
"%PYTHON_EXE%" main.py %*
if errorlevel 1 (
    echo Falha no processamento diario.
    exit /b 1
)
echo =========================================================
echo  Processamento concluido com sucesso!
echo =========================================================
endlocal
