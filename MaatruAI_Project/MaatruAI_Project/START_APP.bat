@echo off
echo.
echo ================================
echo   MatruAI - Startup Script
echo ================================
echo.
echo This script starts both the FastAPI backend and the frontend server.
echo.
echo Requirements:
echo   - Python 3.13+ installed
echo   - FastAPI and Uvicorn installed (pip install fastapi uvicorn)
echo.
echo Starting services on:
echo   - Backend API: http://localhost:8000
echo   - Frontend: http://localhost:3000
echo.
echo ================================
echo.

REM Start FastAPI backend in a new window
echo Starting FastAPI Backend on port 8000...
start "MatruAI Backend - FastAPI" cmd /k python -m uvicorn main:app --reload --port 8000 --host 0.0.0.0

REM Wait a moment for backend to start
timeout /t 3 /nobreak

REM Start Frontend server in another new window
echo Starting Frontend Server on port 3000...
start "MatruAI Frontend" cmd /k python -m http.server 3000 --directory .

REM Wait for both to start
timeout /t 3 /nobreak

REM Open the app in default browser
echo Opening MatruAI in your browser...
start http://localhost:3000/MaatruAI.html

echo.
echo ================================
echo.
echo ✅ Both servers are running!
echo.
echo Frontend: http://localhost:3000/MaatruAI.html
echo Backend API: http://localhost:8000
echo API Documentation: http://localhost:8000/docs
echo.
echo Press Ctrl+C in each terminal to stop the servers.
echo.
echo ================================
