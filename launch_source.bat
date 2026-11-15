@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || goto :bad_directory

if defined MOVIEDATA_PYTHON (
    set "movie_python=%MOVIEDATA_PYTHON%"
    call :probe_python
    if not errorlevel 1 goto :run_python
)

rem Search the path, excluding the Store aliases that only redirect or fail.
for /f "usebackq delims=" %%P in (`"%SystemRoot%\System32\where.exe" py.exe 2^>nul`) do (
    set "movie_python=%%P"
    call :probe_launcher
    if not errorlevel 1 goto :run_launcher
)
for /f "usebackq delims=" %%P in (`"%SystemRoot%\System32\where.exe" python.exe 2^>nul`) do (
    set "movie_python=%%P"
    call :probe_python
    if not errorlevel 1 goto :run_python
)

echo Could not start MovieData. No usable Python 3.10+ with Tkinter was found.
echo Use MovieData.exe from the Windows package or install Python with Tkinter.
echo You can set MOVIEDATA_PYTHON to the full path to python.exe.
set "movie_exit=1"
goto :failed

:probe_python
call :skip_alias
if errorlevel 1 exit /b 1
"%movie_python%" -I -c "import sys, sqlite3, tkinter; sys.exit(sys.version_info < (3,10))" >nul 2>&1
exit /b %errorlevel%

:probe_launcher
call :skip_alias
if errorlevel 1 exit /b 1
"%movie_python%" -3 -I -c "import sys, sqlite3, tkinter; sys.exit(sys.version_info < (3,10))" >nul 2>&1
exit /b %errorlevel%

:skip_alias
for %%P in ("%movie_python%") do if /i "%%~dpP"=="%LOCALAPPDATA%\Microsoft\WindowsApps\" exit /b 1
if not exist "%movie_python%" exit /b 1
exit /b 0

:run_python
"%movie_python%" -E -s -X utf8 "%CD%\gui.py" %*
set "movie_exit=%errorlevel%"
goto :done

:run_launcher
"%movie_python%" -3 -E -s -X utf8 "%CD%\gui.py" %*
set "movie_exit=%errorlevel%"

:done
if "%movie_exit%"=="0" (
    popd
    exit /b 0
)
echo.
echo MovieData exited with code %movie_exit%.

:failed
popd
if not defined MOVIEDATA_NO_PAUSE pause
exit /b %movie_exit%

:bad_directory
echo Unable to open the extracted MovieData folder.
if not defined MOVIEDATA_NO_PAUSE pause
exit /b 1
