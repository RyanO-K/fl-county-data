# Registers (or updates) a Windows Task Scheduler job that runs the
# Florida county data ETL once per week, Sunday at 3:00 AM local time.
# (Daily until 2026-09-17: a full refresh can take most of a day once a
# county's ArcGIS service degrades, so nightly runs kept getting cut off.)
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
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 3:00AM
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 12)

Register-ScheduledTask -TaskName "FLCountyDataETL" `
    -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Weekly sync of Florida county parcel/zoning/land-use GIS data plus statewide DOR parcel values, owners and recorded instruments into local SQLite DB" `
    -Force

Write-Host "Scheduled task 'FLCountyDataETL' registered: weekly, Sunday 3:00 AM, running $EtlScript"
