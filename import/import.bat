@echo off
rem DetourAI: one-time setup and map import on Windows with Docker Desktop.
rem   import\import.bat              downloads the map if missing, starts the database, creates the tables, imports the city
rem   import\import.bat PATH.osm.pbf  the same with your own extract
rem Reads DB_PASSWORD (and BBOX, if you set one) from .env, so it always matches the database.
setlocal EnableExtensions
cd /d "%~dp0\.."

if not exist .env (
  copy .env.example .env >nul
  echo Created .env from .env.example. Change DB_PASSWORD and ADMIN_TOKEN in it before anyone else can reach this machine.
)
set "DB_PASSWORD=detour"
set "BBOX=78.20,17.20,78.75,17.65"
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do if not "%%B"=="" set "%%A=%%B"
set "PBF=data\southern-zone-latest.osm.pbf"
if not "%~1"=="" set "PBF=%~1"

if not exist data mkdir data
if not exist "%PBF%" (
  echo == downloading the map, about 560 MB
  curl -L --fail -o "%PBF%" https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf || goto :fail
)

echo == starting the database
docker compose -p detourai up -d --build db || goto :fail
for /l %%i in (1,1,60) do (
  docker compose -p detourai exec -T db pg_isready -U detour -d detour >nul 2>&1 && goto :ready
  timeout /t 2 /nobreak >nul
)
echo The database did not become ready. Look at: docker compose -p detourai logs db --tail 40
goto :fail

:ready
echo == creating the tables
docker compose -p detourai exec -T db bash /migrate.sh || goto :fail

echo == importing the road network (20 to 60 minutes on the full city; this window shows progress)
rem The fixed name stops a second import from starting while one is running: two at once corrupt each other.
docker run --rm --name detourai-import --network detourai_default -v "%cd%":/work -w /work -e DEBIAN_FRONTEND=noninteractive -e BBOX=%BBOX% -e DATABASE_URL=postgresql://detour:%DB_PASSWORD%@db:5432/detour debian:trixie bash -c "apt-get update -qq && apt-get install -y -qq osmium-tool osm2pgsql postgresql-client >/dev/null 2>&1 && bash import/import_osm.sh %PBF:\=/%"
if errorlevel 1 (
  echo.
  echo If the message says the name detourai-import is already in use, an import is still running:
  echo   docker logs -f detourai-import
  goto :fail
)

echo == starting the API and the worker
docker compose -p detourai up -d --build || goto :fail
echo.
echo Done. Open http://127.0.0.1:8080/console and sign in with the ADMIN_TOKEN from .env, then run verify\verify.bat
exit /b 0

:fail
echo.
echo A step failed. Copy the lines above this message and send them.
exit /b 1
