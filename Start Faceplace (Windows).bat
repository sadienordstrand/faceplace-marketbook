@echo off
rem Faceplace Marketbook launcher for Windows. Double-click it.
rem
rem First run installs what it needs into a .venv folder next to this file and
rem downloads the browser it drives. Later runs skip straight to the search
rem window. Nothing is installed outside this folder.

setlocal
cd /d "%~dp0"

rem Keeps accented characters in listing titles from tripping up output,
rem whatever the machine's regional settings are.
set "PYTHONUTF8=1"

rem Keeps pip's "a new release is available" advice out of a window aimed at
rem someone who doesn't need to hear it.
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

rem --- 1. Find a usable Python ------------------------------------------------
rem The Microsoft Store stub named "python" fails this check, which is what we
rem want: it can't actually run anything.
set "PY="
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY goto no_python

rem --- 2. Create the private Python folder ------------------------------------
set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" (
    echo First-time setup. This takes a minute or two...
    if exist .venv rmdir /s /q .venv
    %PY% -m venv .venv
    if errorlevel 1 goto venv_failed
)

rem --- 3. Install dependencies, but only when they change ---------------------
rem The stamp file is a copy of requirements.txt, so editing that file is what
rem triggers a reinstall.
fc /b requirements.txt ".venv\.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing the browser automation library...
    "%VPY%" -m pip install --quiet --upgrade pip >nul 2>&1
    "%VPY%" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto pip_failed
    copy /y requirements.txt ".venv\.installed" >nul
    rem A new Playwright pins a new Chromium build, so re-check the browser too.
    if exist ".venv\.browser-installed" del ".venv\.browser-installed"
)

rem --- 4. Download the browser Playwright drives ------------------------------
if not exist ".venv\.browser-installed" (
    echo Downloading the browser it drives ^(about 150 MB, one time only^)...
    "%VPY%" -m playwright install chromium
    if errorlevel 1 goto browser_failed
    echo installed> ".venv\.browser-installed"
)

rem --- 5. Go -----------------------------------------------------------------
"%VPY%" "src\fb_marketplace_sweep.py" %*
set "STATUS=%ERRORLEVEL%"

echo.
if not "%STATUS%"=="0" echo Faceplace exited with an error ^(code %STATUS%^). The messages above say why.
pause
exit /b %STATUS%

:no_python
echo.
echo Faceplace needs Python 3.9 or newer, and it isn't installed yet.
echo.
echo   1. Open  https://www.python.org/downloads/windows/
echo   2. Download the latest "Windows installer (64-bit)".
echo   3. Run it. On the first screen, TICK THE BOX that says
echo      "Add python.exe to PATH", then click Install Now.
echo   4. Double-click this Start Faceplace file again.
echo.
echo That's a one-time install. Nothing else to set up.
pause
exit /b 1

:venv_failed
echo.
echo Could not create the .venv folder. Is this folder read-only,
echo or is it inside a synced folder that is currently locked?
pause
exit /b 1

:pip_failed
echo.
echo Install failed. Check your internet connection and try again.
pause
exit /b 1

:browser_failed
echo.
echo Browser download failed. Check your internet connection and try again.
pause
exit /b 1
