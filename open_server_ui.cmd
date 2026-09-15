@echo off
title ESG Server Tunnel - KEEP THIS WINDOW OPEN
echo ==========================================================
echo   ESG Server UI  (via SSH tunnel, no public exposure)
echo   Server: 124.222.244.77
echo ==========================================================
echo.
echo   Browser will open http://localhost:18080 in 5 seconds.
echo   Close THIS window to disconnect.
echo.
start "esg-opener" /min cmd /c "timeout /t 5 >nul & start http://localhost:18080"
ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -N -L 18080:127.0.0.1:80 ubuntu@124.222.244.77
echo.
echo Tunnel closed. Press any key to exit.
pause >nul
