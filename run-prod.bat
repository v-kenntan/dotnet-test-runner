@echo off
title .NET SDK Test Runner (Production)
echo Building and starting in production mode...

:: Build frontend (vite outputs straight to backend/static)
cd frontend
call npm run build
cd ..

:: Start backend (serves both API and static frontend)
echo.
echo App running at: http://localhost:5000
echo.
cd backend
python app.py
