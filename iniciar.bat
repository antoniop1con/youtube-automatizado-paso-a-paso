@echo off
REM ============================================================
REM  Lanzador de la app. Antes de la primera vez, rellena las
REM  3 claves de abajo (entre comillas quita las mayusculas de
REM  ejemplo y pega tu clave real). Luego solo hay que hacer
REM  doble clic en este archivo para arrancar la app.
REM ============================================================

set GEMINI_API_KEY=PEGA_AQUI_TU_CLAVE_DE_GEMINI
set ELEVENLABS_API_KEY=PEGA_AQUI_TU_CLAVE_DE_ELEVENLABS
set ELEVENLABS_VOICE_ID=PEGA_AQUI_EL_VOICE_ID

cd /d "%~dp0"
py app.py

echo.
echo (La ventana se ha cerrado o la app ha fallado. Lee el mensaje de arriba.)
pause
