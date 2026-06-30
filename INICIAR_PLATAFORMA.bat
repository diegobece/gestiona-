@echo off
chcp 65001 >nul
title Detección de pagos sin factura - Plataforma
cd /d "%~dp0backend"

echo ============================================================
echo   Deteccion de pagos sin factura  -  v1
echo ============================================================
echo.
echo Comprobando dependencias (solo tarda la primera vez)...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] No se pudieron instalar las dependencias.
  echo Asegurate de tener Python instalado y en el PATH.
  pause
  exit /b 1
)

echo.
echo Iniciando el servidor en http://localhost:8011
echo El navegador se abrira automaticamente en unos segundos.
echo Para detener la plataforma: cierra esta ventana o pulsa Ctrl+C.
echo ============================================================

rem Abre el navegador tras un breve retardo, cuando el servidor ya escucha.
start "" cmd /c "timeout /t 3 >nul & start http://localhost:8011"

python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8011
pause
