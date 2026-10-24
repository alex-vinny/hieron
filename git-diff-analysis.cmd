@echo off
setlocal
rem This script is Python 3.8+. Never call bare `python` here: on a machine with a
rem legacy Python first on PATH, `python` resolves to 2.7, which dies on the first
rem non-ASCII byte with `SyntaxError: Non-ASCII character '\xe2'`.
rem Prefer the PEP 397 launcher, fall back to `python3`.
where py >nul 2>nul
if errorlevel 1 goto :nopy
py -3 "%~dp0git_diff_analysis.py" %*
exit /b %errorlevel%
:nopy
python3 "%~dp0git_diff_analysis.py" %*
exit /b %errorlevel%
