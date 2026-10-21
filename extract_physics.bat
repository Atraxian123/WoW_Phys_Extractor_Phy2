@echo off
setlocal
set "PYCMD="
set "SCRIPT="
set "MODERN=%~dp0m2_extract_phys.py"
set "LEGACY=%~dp0m2_extract_phys_py26.py"

rem ---- 1) Modern Python (3.7 or newer) -> m2_extract_phys.py
rem      py launcher first, newest version first
where py >nul 2>nul
if not errorlevel 1 (
  for %%V in (3 3.14 3.13 3.12 3.11 3.10 3.9 3.8 3.7) do (
    if not defined PYCMD (
      py -%%V -c "import sys; sys.exit(0 if sys.version_info >= (3,7) else 1)" >nul 2>nul
      if not errorlevel 1 set "PYCMD=py -%%V"
    )
  )
)
rem      then python / python3 on PATH
for %%C in (python python3) do (
  if not defined PYCMD (
    where %%C >nul 2>nul
    if not errorlevel 1 (
      %%C -c "import sys; sys.exit(0 if sys.version_info >= (3,7) else 1)" >nul 2>nul
      if not errorlevel 1 set "PYCMD=%%C"
    )
  )
)
if defined PYCMD set "SCRIPT=%MODERN%"

rem ---- 2) Older Python (2.6 / 2.7, or 3.0 - 3.6) -> m2_extract_phys_py26.py
if not defined PYCMD (
  where py >nul 2>nul
  if not errorlevel 1 (
    for %%V in (2.7 2.6 2 3) do (
      if not defined PYCMD (
        py -%%V -c "import sys; sys.exit(0 if sys.version_info >= (2,6) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -%%V"
      )
    )
  )
)
if not defined PYCMD (
  for %%C in (python python3) do (
    if not defined PYCMD (
      where %%C >nul 2>nul
      if not errorlevel 1 (
        %%C -c "import sys; sys.exit(0 if sys.version_info >= (2,6) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=%%C"
      )
    )
  )
)
if not defined PYCMD (
  for %%P in (C:\Python27\python.exe C:\Python26\python.exe) do (
    if not defined PYCMD (
      if exist %%P (
        %%P -c "import sys; sys.exit(0 if sys.version_info >= (2,6) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYCMD=%%P"
      )
    )
  )
)
if not defined SCRIPT if defined PYCMD set "SCRIPT=%LEGACY%"

if not defined PYCMD (
  echo No usable Python was found ^(needs 2.6 or newer^).
  echo.
  echo What the py launcher sees:
  py -0p 2>nul || echo   ^(py launcher not installed^)
  echo.
  echo What is on PATH:
  where python python3 2>nul || echo   ^(none^)
  echo.
  echo Install Python from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist "%SCRIPT%" (
  echo Missing script: %SCRIPT%
  echo Keep m2_extract_phys.py and m2_extract_phys_py26.py in the same folder as this .bat file.
  pause
  exit /b 1
)

%PYCMD% "%SCRIPT%" %*
if errorlevel 1 pause
