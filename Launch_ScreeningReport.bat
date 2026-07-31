@echo off
setlocal

set "INSTALL_DIR=C:\Users\supervisor.GLACIOS-9961023\Documents\sean"
set "PROJECT_DIR=%INSTALL_DIR%"
set "PYTHON=%INSTALL_DIR%\.venv\Scripts\python.exe"

rem Locate the folder containing the ScreeningReport source code.
if exist "%INSTALL_DIR%\SmartScreeningReport\screening_report\__main__.py" (
    set "PROJECT_DIR=%INSTALL_DIR%\SmartScreeningReport"
) else if exist "%INSTALL_DIR%\ScreeningReport\screening_report\__main__.py" (
    set "PROJECT_DIR=%INSTALL_DIR%\ScreeningReport"
)
if not exist "%PYTHON%" (
    if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
        set "PYTHON=%PROJECT_DIR%\.venv\Scripts\python.exe"
    )
)
if not exist "%PYTHON%" (
    if exist "%INSTALL_DIR%\venv\Scripts\python.exe" (
        set "PYTHON=%INSTALL_DIR%\venv\Scripts\python.exe"
    )
)

if not exist "%PYTHON%" goto python_missing
if not exist "%PROJECT_DIR%\screening_report\__main__.py" goto project_missing

cd /d "%PROJECT_DIR%"
echo Project folder: %PROJECT_DIR%
echo Python: %PYTHON%
echo Checking application modules...
"%PYTHON%" -c "import screening_report; print('Package:', screening_report.__file__); import screening_report.acquisition_metadata; import screening_report.gui"
if errorlevel 1 goto import_failed

echo Starting ScreeningReport...
"%PYTHON%" -m screening_report
set "RESULT=%ERRORLEVEL%"

if "%RESULT%"=="0" exit /b 0

echo.
echo ScreeningReport stopped with error code %RESULT%.
echo The error shown above explains what prevented it from opening.
echo.
pause
exit /b %RESULT%

:python_missing
echo.
echo Could not find the virtual environment's python.exe.
echo Checked:
echo   %INSTALL_DIR%\.venv\Scripts\python.exe
echo   %PROJECT_DIR%\.venv\Scripts\python.exe
echo   %INSTALL_DIR%\venv\Scripts\python.exe
echo.
pause
exit /b 1

:import_failed
echo.
echo The ScreeningReport source folder is incomplete or contains mixed versions.
echo Copy the entire "screening_report" folder into:
echo   %PROJECT_DIR%
echo.
echo Do not update only individual Python files.
echo Then update the existing environment with:
echo   "%PYTHON%" -m pip install -r "%PROJECT_DIR%\requirements.txt"
echo.
pause
exit /b 1

:project_missing
echo.
echo Could not find the ScreeningReport program files.
echo Expected:
echo   %INSTALL_DIR%\screening_report\__main__.py
echo or:
echo   %INSTALL_DIR%\SmartScreeningReport\screening_report\__main__.py
echo or:
echo   %INSTALL_DIR%\ScreeningReport\screening_report\__main__.py
echo.
pause
exit /b 1
