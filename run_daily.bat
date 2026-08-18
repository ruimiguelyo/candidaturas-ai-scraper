@echo off
cd /d "%~dp0"
echo =========================================================
echo  Agregador Diario de Vagas IA - Junior / Trainee / Estagio
echo =========================================================
python main.py
if errorlevel 1 (
    echo Falha no processamento diario.
    exit /b 1
)
echo =========================================================
echo  Processamento concluido com sucesso!
echo =========================================================
