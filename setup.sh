@echo off
REM Script de inicio rápido para el bot de Discord (Windows)
REM Este script te ayudará a configurar el bot rápidamente

echo ======================================
echo   Bot de Discord - Inicio Rápido
echo ======================================
echo.

REM Verificar Python
echo [*] Verificando Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python no está instalado. Por favor, instálalo primero.
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do set PYTHON_VERSION=%%i
echo [OK] %PYTHON_VERSION% encontrado
echo.

REM Verificar FFmpeg
echo [*] Verificando FFmpeg...
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [!] FFmpeg no está instalado.
    echo     Instrucciones de instalación:
    echo     - Descarga desde: https://ffmpeg.org/download.html
    echo     - O usa Chocolatey: choco install ffmpeg
    echo.
    set /p response="Deseas continuar sin FFmpeg? (las funciones de musica no funcionaran) [y/N]: "
    if /i not "%response%"=="y" exit /b 1
) else (
    echo [OK] FFmpeg encontrado
)
echo.

REM Crear entorno virtual
echo [*] Configurando entorno virtual...
if not exist "venv" (
    python -m venv venv
    echo [OK] Entorno virtual creado
) else (
    echo [OK] Entorno virtual ya existe
)
echo.

REM Activar entorno virtual
echo [*] Activando entorno virtual...
call venv\Scripts\activate.bat
echo [OK] Entorno virtual activado
echo.

REM Instalar dependencias
echo [*] Instalando dependencias...
pip install -r requirements.txt
echo [OK] Dependencias instaladas
echo.

REM Configurar .env
if not exist ".env" (
    echo [*] Configurando archivo .env...
    copy .env.example .env
    echo [OK] Archivo .env creado
    echo.
    echo [!] IMPORTANTE: Edita el archivo .env con tus credenciales:
    echo     - TOKEN: Tu token del bot de Discord
    echo     - ADMIN_LOG_CHANNEL_ID: ID del canal de logs
    echo.
    set /p edit_env="Deseas editar el archivo .env ahora? [y/N]: "
    if /i "%edit_env%"=="y" notepad .env
) else (
    echo [OK] Archivo .env ya existe
)
echo.

REM Instrucciones finales
echo ======================================
echo   [OK] Configuracion completada
echo ======================================
echo.
echo Proximos pasos:
echo.
echo 1. Asegurate de haber configurado tu .env con:
echo    - TOKEN del bot
echo    - ADMIN_LOG_CHANNEL_ID
echo.
echo 2. Activa el entorno virtual (si no esta activo):
echo    venv\Scripts\activate.bat
echo.
echo 3. Ejecuta el bot:
echo    python bot.py
echo.
echo Para mas informacion, consulta README.md
echo.
pause