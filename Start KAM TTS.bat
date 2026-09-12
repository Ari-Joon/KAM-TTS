@echo off
setlocal
title KAM TTS server
rem Starts the server from wherever this project lives, with Python 3.10 if the
rem py launcher can find it and plain "python" otherwise. Nothing here names a
rem folder or an interpreter path, so it works on any machine and after a move.
rem The server repairs the power button's registration as it boots.
cd /d "%~dp0server"
set "PYCMD="
py -3.10 -c "import sys" >nul 2>nul && set "PYCMD=py -3.10"
if not defined PYCMD set "PYCMD=python"
echo Starting KAM TTS with %PYCMD% ... this takes about a minute the first time.
%PYCMD% server.py
echo.
echo Server stopped. Press any key to close.
pause >nul
