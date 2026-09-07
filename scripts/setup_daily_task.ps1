# Registers (or updates) a Windows Task Scheduler job that runs the
# Florida county data ETL once per day at 3:00 AM local time.
#
# Run this manually once from an elevated or normal PowerShell prompt:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_daily_task.ps1
#
# To remove the task later:
#   schtasks /delete /tn "FLCountyDataETL" /f

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe  = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$EtlScript  = Join-Path $ProjectDir "scripts\etl.py"

$Action  = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$EtlScript`"" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At 3:00AM
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 12)

Register-ScheduledTask -TaskName "FLCountyDataETL" `
    -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Daily sync of Florida county parcel/zoning/land-use GIS data plus statewide DOR parcel values into local SQLite DB" `
    -Force

Write-Host "Scheduled task 'FLCountyDataETL' registered: daily at 3:00 AM, running $EtlScript"
