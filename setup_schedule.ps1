# setup_schedule.ps1
# Registers a Windows Task Scheduler task to run fetch_tlv_addresses.py
# every Sunday at 03:00. Run this script once as Administrator.

$taskName   = "FetchTLVAddresses"
$scriptDir  = $PSScriptRoot
$scriptPath = Join-Path $scriptDir "fetch_tlv_addresses.py"

# Resolve the python executable (uses whichever python is on PATH)
$pythonPath = (Get-Command python -ErrorAction Stop).Source

$action  = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument "`"$scriptPath`"" `
    -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "03:00"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "Task '$taskName' registered successfully."
Write-Host "It will run every Sunday at 03:00 using: $pythonPath"
Write-Host "Script path: $scriptPath"
