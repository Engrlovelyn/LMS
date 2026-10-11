@echo off
REM Opens the Django server and the Cloudflare tunnel in two windows.
echo Starting TASS LMS demo (server + tunnel)...
start "TASS LMS Server" cmd /k "%~dp0start_server.bat"
echo Waiting for the server to boot...
timeout /t 6 /nobreak >nul
start "Cloudflare Tunnel" cmd /k "%~dp0start_tunnel.bat"
echo.
echo Two windows opened:
echo   1. TASS LMS Server  (must stay open)
echo   2. Cloudflare Tunnel (copy the trycloudflare.com URL from here)
