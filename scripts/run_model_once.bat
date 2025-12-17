@echo off
cd /d "%~dp0\.."
C:\Users\jttre\AppData\Local\Microsoft\WindowsApps\python3.13.exe model\run_model.py >> logs\run_model.log 2>&1
