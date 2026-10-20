@echo off
where py >nul 2>nul && (py "%~dp0m2_extract_phys.py" %* & goto :end)
python "%~dp0m2_extract_phys.py" %*
:end
if errorlevel 1 pause
