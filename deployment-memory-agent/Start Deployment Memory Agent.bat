@echo off
setlocal
cd /d "%~dp0"
if not exist "backend\.venv\Scripts\python.exe" (
  python -m venv backend\.venv
  call backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
)
if not exist "backend\.env" if exist "backend\.env.example" copy "backend\.env.example" "backend\.env" >nul
if not exist "frontend\node_modules\vite\bin\vite.js" (
  cd frontend
  call npm install
  cd ..
)
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if errorlevel 1 start "Deployment API" /min cmd /c "cd /d ""%~dp0backend"" && "".venv\Scripts\python.exe"" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
netstat -ano | findstr /R /C:":5173 .*LISTENING" >nul
if errorlevel 1 start "Deployment UI" /min cmd /c "cd /d ""%~dp0frontend"" && call npm run dev -- --host 127.0.0.1"
powershell -NoProfile -Command "Start-Sleep -Seconds 4"
start "" "http://127.0.0.1:5173"
