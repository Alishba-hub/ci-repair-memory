@echo off
title CI Memory Agents
cd /d "%~dp0"
echo.
echo   Starting the experiment runner...
echo   Your browser will open automatically.
echo.
echo   Leave this window open while you work.
echo   Close it, or press Ctrl+C, when you are finished.
echo.
python scripts\dashboard.py
pause
