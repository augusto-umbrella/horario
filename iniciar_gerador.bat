@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Criando ambiente Python...
    py -3 -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERRO: nao foi possivel criar o ambiente. Instale o Python 3 para Windows.
        pause
        exit /b 1
    )
)

if not exist ".venv\dependencias_ok.txt" (
    echo Instalando dependencias. Na primeira vez pode demorar alguns minutos...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto erro
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto erro
    echo OK>".venv\dependencias_ok.txt"
)

echo Iniciando Gerador de Horarios Escolares...
".venv\Scripts\python.exe" -m streamlit run app.py
goto fim

:erro
echo.
echo ERRO ao instalar as dependencias. Verifique sua conexao com a internet.
pause
exit /b 1

:fim
endlocal
