@echo off
rem DetourAI: apply a downloaded update zip to this repo, show what changed, then commit and push.
rem Usage: double-click, or run  apply-update.bat  (newest detourai-*update*.zip in Downloads)
rem        or  apply-update.bat "C:\path\to\some-update.zip"
setlocal enabledelayedexpansion
cd /d "%~dp0"
if not exist ".git" (
  echo This script must sit inside the Detour_AI repo folder.
  pause
  exit /b 1
)

set "DL=%USERPROFILE%\Downloads"
set "ZIP="
if not "%~1"=="" set "ZIP=%~1"
if not defined ZIP (
  for /f "delims=" %%F in ('dir /b /o-d "%DL%\detourai-*update*.zip" 2^>nul') do (
    if not defined ZIP set "ZIP=%DL%\%%F"
  )
)
if not defined ZIP (
  echo No detourai update zip found in %DL%.
  echo Download the update zip first, then run this again.
  pause
  exit /b 1
)
if not exist "%ZIP%" (
  echo File not found: %ZIP%
  pause
  exit /b 1
)

echo.
echo Applying: %ZIP%
tar -xf "%ZIP%"
if errorlevel 1 (
  echo Could not unpack the zip. Nothing was committed.
  pause
  exit /b 1
)

rem files an update removes are listed one per line in .update-delete.txt
if exist ".update-delete.txt" (
  for /f "usebackq delims=" %%D in (".update-delete.txt") do (
    set "P=%%D"
    set "P=!P:/=\!"
    if exist "!P!" del /q "!P!"
  )
  del /q ".update-delete.txt"
)

set "MSG=DetourAI update"
if exist ".update-message.txt" (
  set /p MSG=<".update-message.txt"
  del /q ".update-message.txt"
)

git status --porcelain > "%TEMP%\detourai-status.txt"
for %%A in ("%TEMP%\detourai-status.txt") do set "SIZE=%%~zA"
if "%SIZE%"=="0" (
  echo.
  echo Nothing changed. This update is already applied.
  pause
  exit /b 0
)

echo.
echo Files changed by this update:
git status --short
echo.
echo Commit message: %MSG%
echo.
set "OK="
set /p OK=Commit and push these changes? (Y/N) 
if /i not "%OK%"=="Y" (
  echo Stopped. The files are updated in this folder; nothing was committed or pushed.
  pause
  exit /b 0
)

git add -A
git commit -m "%MSG%"
if errorlevel 1 (
  echo Commit failed. See the message above.
  pause
  exit /b 1
)
git push
if errorlevel 1 (
  echo Push failed. The commit is saved locally; run  git push  again once the problem is fixed.
  pause
  exit /b 1
)
echo.
echo Done. The update is on GitHub.
pause
