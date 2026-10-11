@echo off
rem Prepara o ambiente (na primeira vez) e inicia o sistema.
rem Uso: iniciar.bat        -> inicia
rem      iniciar.bat demo   -> cria dados de demonstracao antes de iniciar
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo Criando ambiente virtual...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist .venv\Scripts\python.exe (
        echo.
        echo Nao encontrei o Python. Instale o Python 3.10 ou mais novo em https://www.python.org/downloads/
        echo e marque a opcao "Add python.exe to PATH" durante a instalacao.
        pause
        exit /b 1
    )
)

echo Conferindo dependencias...
.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Falha ao instalar as dependencias. Confira a conexao com a internet.
    pause
    exit /b 1
)

if /i "%~1"=="demo" .venv\Scripts\python.exe demo.py

.venv\Scripts\python.exe app.py
pause
