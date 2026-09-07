@echo off
title CI Memory Agents
cd /d "%~dp0"
echo.
echo   CI Memory Agents
echo   ----------------
echo.
echo   Checking this machine can run the experiment...
echo.
python run.py doctor
echo.
echo   Next:
echo     python run.py tasks     see what there is to run
echo     python run.py           run everything
echo     python run.py status    check progress
echo.
pause
