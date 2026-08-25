@echo off
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (set PY=py -3) else (set PY=python)

%PY% -c "import customtkinter" >nul 2>&1
if errorlevel 1 (
    echo [GameSay] First run: installing customtkinter ...
    %PY% -m pip install customtkinter
)

%PY% game_say.py
if errorlevel 1 pause
