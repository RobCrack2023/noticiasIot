@echo off
echo Iniciando TechPulse - Noticias IoT y Robotica...
echo Abre tu navegador en: http://localhost:8000
uvicorn main:app --reload --host 0.0.0.0 --port 8000
pause
