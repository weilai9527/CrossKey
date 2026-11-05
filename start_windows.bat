@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0" || goto directory_error

set "CROSSKEY_PY_ARGS="
if exist ".venv\Scripts\pythonw.exe" (
    set "CROSSKEY_PYTHON=%~dp0.venv\Scripts\pythonw.exe"
    goto launch_gui
)
if exist "venv\Scripts\pythonw.exe" (
    set "CROSSKEY_PYTHON=%~dp0venv\Scripts\pythonw.exe"
    goto launch_gui
)
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
    set "CROSSKEY_PYTHON=%%~fP"
    goto launch_gui
)
for /f "delims=" %%P in ('where pyw.exe 2^>nul') do (
    set "CROSSKEY_PYTHON=%%~fP"
    set "CROSSKEY_PY_ARGS=-3"
    goto launch_gui
)
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    set "CROSSKEY_PYTHON=%%~fP"
    goto launch_console
)
for /f "delims=" %%P in ('where py.exe 2^>nul') do (
    set "CROSSKEY_PYTHON=%%~fP"
    set "CROSSKEY_PY_ARGS=-3"
    goto launch_console
)
echo CrossKey could not find Python.
echo Install Python 3.10 or newer from python.org with Tcl/Tk and Add Python to PATH enabled.
echo Then run: python -m pip install -r requirements.txt
pause
exit /b 1

:launch_gui
rem Check prerequisites before detaching, so missing Tk never fails silently.
start "" /wait "%CROSSKEY_PYTHON%" %CROSSKEY_PY_ARGS% -c "import sys, tkinter, launcher; assert sys.version_info >= (3, 10)"
if errorlevel 1 goto prerequisites_error
start "" "%CROSSKEY_PYTHON%" %CROSSKEY_PY_ARGS% "%~dp0launcher.py"
if errorlevel 1 goto launch_error
exit /b 0

:launch_console
"%CROSSKEY_PYTHON%" %CROSSKEY_PY_ARGS% "%~dp0launcher.py"
if errorlevel 1 goto launch_error
exit /b 0

:prerequisites_error
echo CrossKey needs Python 3.10 or newer with Tcl/Tk support.
echo If dependencies are missing, run: python -m pip install -r requirements.txt
echo For the full error, run: python launcher.py
echo If Python is installed through the launcher, use py -3 instead of python.
pause
exit /b 1

:launch_error
echo CrossKey could not start. Run python launcher.py to see the full error.
echo If dependencies are missing, run: python -m pip install -r requirements.txt
pause
exit /b 1

:directory_error
echo CrossKey could not open its project folder. Move the project to a local folder and retry.
pause
exit /b 1
