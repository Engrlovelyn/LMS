@echo off
cd /d "%~dp0.."
echo ============================================
echo  TASS LMS - Django dev server
echo  Local:  http://localhost:8000
echo  LAN:    http://10.94.72.168:8000
echo  (Keep this window OPEN during the demo)
echo ============================================
python manage.py runserver 0.0.0.0:8000
