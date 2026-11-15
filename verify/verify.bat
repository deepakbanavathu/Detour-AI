@echo off
rem DetourAI verification on Windows with Docker Desktop. Run from anywhere; it works from the repo root.
rem   verify.bat                     full run: graph, vehicles, routes, OSRM, GPX, advisories, latency, re-import, worker restart, Osmose
rem   verify.bat --skip-reimport     same, without rebuilding the network (saves 10 to 20 minutes on the full city)
rem   verify.bat review              build a blind review pack (verify\review\<date>\review.html)
rem   verify.bat score ANSWERS KEY   score the reviewer's CSV against the key, into the last report
rem Exit code: 0 all passed or known issues only, 1 a criterion failed, 2 a step or the script failed.
setlocal EnableExtensions
cd /d "%~dp0\.."

set "PROJECT=detourai"
set "NET=detourai_default"
set "OSRM_IMAGE=ghcr.io/project-osrm/osrm-backend:v5.27.1"
set "PBF=data\southern-zone-latest.osm.pbf"
set "BBOX=78.20,17.20,78.75,17.65"
set "DB_PASSWORD=detour"
set "ADMIN_TOKEN=change-me"
if exist .env for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do if not "%%B"=="" set "%%A=%%B"
set "GIT=unknown"
for /f %%G in ('git rev-parse --short HEAD 2^>nul') do set "GIT=%%G"

if not exist verify\inputs\gpx mkdir verify\inputs\gpx
if not exist verify\work mkdir verify\work
if not exist verify\reports mkdir verify\reports
for %%F in (pairs.csv gates.csv advisories.geojson) do if not exist "verify\inputs\%%F" copy /y "verify\templates\%%F" "verify\inputs\%%F" >nul

set "RUNV=docker run --rm --network %NET% -v "%cd%":/work -e DATABASE_URL=postgresql://detour:%DB_PASSWORD%@db:5432/detour -e DETOUR_API=http://api:8080 -e OSRM_URL=http://detour-osrm:5000 -e ADMIN_TOKEN=%ADMIN_TOKEN% -e DETOUR_GIT=%GIT% detourai-verify python verify/verify.py"

echo == building the verification image
docker build -q -t detourai-verify verify >nul || goto :fail

if /i "%~1"=="review" goto :review
if /i "%~1"=="score" goto :score

echo == 1/7 stack up (the database restarts once to load pg_stat_statements)
docker compose -p %PROJECT% up -d --build || goto :fail
call :wait_api || goto :fail

echo == 2/7 OSRM built from the same OSM file and area
call :osrm || goto :fail

echo == 3/7 checks against the database, the API and your inputs
%RUNV% run %* || goto :fail
set /p REPORT=<verify\work\current_report.txt

echo == 4/7 worker killed mid-poll, then started again
%RUNV% restart-before --report %REPORT% || goto :fail
timeout /t 2 /nobreak >nul
docker compose -p %PROJECT% kill -s SIGKILL worker >nul
docker compose -p %PROJECT% up -d worker >nul || goto :fail
%RUNV% restart-after --report %REPORT% || goto :fail

echo == 5/7 Osmose issues for the area (needs internet; a failure here does not stop the run)
%RUNV% osmose --report %REPORT%

echo == 6/7 summary
%RUNV% finalize --report %REPORT%
set "RC=%errorlevel%"
echo == 7/7 report: verify\reports\%REPORT:/work/verify/reports/=%
exit /b %RC%

:review
call :wait_api || goto :fail
call :osrm || goto :fail
%RUNV% review-pack --report /work/verify/work/review-report.json --n 10 || goto :fail
echo Send only review.html from the newest folder in verify\review. Keep KEY-do-not-share.json to yourself.
exit /b 0

:score
if "%~3"=="" ( echo usage: verify.bat score verify\review\FOLDER\review-answers.csv verify\review\FOLDER\KEY-do-not-share.json & exit /b 2 )
set "ANS=%~2"
set "KEY=%~3"
set /p REPORT=<verify\work\current_report.txt
%RUNV% score-review --report %REPORT% --answers /work/%ANS:\=/% --key /work/%KEY:\=/% || goto :fail
%RUNV% finalize --report %REPORT%
exit /b %errorlevel%

:wait_api
for /l %%i in (1,1,60) do (
  docker run --rm --network %NET% detourai-verify python -c "import httpx,sys; sys.exit(0 if httpx.get('http://api:8080/healthz', timeout=5).status_code == 200 else 1)" >nul 2>&1 && exit /b 0
  timeout /t 3 /nobreak >nul
)
echo The API did not answer on /healthz. Look at: docker compose -p %PROJECT% logs api --tail 60
exit /b 1

:osrm
if not exist "verify\work\city.osrm.mldgr" (
  if not exist "%PBF%" ( echo %PBF% is missing. Download it first: see verify\VERIFY.md, section 3. & exit /b 1 )
  docker run --rm -v "%cd%":/work -w /work debian:trixie bash -c "apt-get update -qq && apt-get install -y -qq osmium-tool >/dev/null && osmium extract --bbox %BBOX% --strategy complete_ways %PBF:\=/% -o verify/work/city.osm.pbf --overwrite" || exit /b 1
  docker run --rm -v "%cd%\verify\work":/data %OSRM_IMAGE% osrm-extract -p /opt/car.lua /data/city.osm.pbf || exit /b 1
  docker run --rm -v "%cd%\verify\work":/data %OSRM_IMAGE% osrm-partition /data/city.osrm || exit /b 1
  docker run --rm -v "%cd%\verify\work":/data %OSRM_IMAGE% osrm-customize /data/city.osrm || exit /b 1
)
docker rm -f detour-osrm >nul 2>&1
docker run -d --name detour-osrm --network %NET% -v "%cd%\verify\work":/data %OSRM_IMAGE% osrm-routed --algorithm mld /data/city.osrm >nul || exit /b 1
exit /b 0

:fail
echo.
echo A step failed. Copy the lines above this message and send them.
exit /b 2
