@echo off
REM Stream the real online Claude Max usage to the STM32 OLED.
REM Double-click to run. Edit COM5 below if your USB-TTL adapter uses another port.

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0claude_max.py" --port COM5
pause
