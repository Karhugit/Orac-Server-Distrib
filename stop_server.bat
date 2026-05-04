@echo off
echo Stopping Orac Server...
powershell.exe -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*run_server.py*'} | Invoke-CimMethod -MethodName Terminate | Out-Null"
echo Server stopped.
pause
