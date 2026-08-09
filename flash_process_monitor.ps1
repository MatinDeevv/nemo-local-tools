$ErrorActionPreference = 'Stop'

$logPath = 'C:\Users\marti\AppData\Local\Temp\codex-flash-process-monitor.jsonl'
$deadline = (Get-Date).AddMinutes(2)

Remove-Item -LiteralPath $logPath -Force -ErrorAction SilentlyContinue
"monitor-started $(Get-Date -Format o)" | Set-Content -LiteralPath 'C:\Users\marti\AppData\Local\Temp\codex-flash-process-monitor.status'
$known = [System.Collections.Generic.HashSet[int]]::new()
[System.Diagnostics.Process]::GetProcesses() | ForEach-Object {
    $null = $known.Add($_.Id)
    $_.Dispose()
}

while ((Get-Date) -lt $deadline) {
    foreach ($newProcess in [System.Diagnostics.Process]::GetProcesses()) {
        $pidValue = $newProcess.Id
        if (-not $known.Add($pidValue)) {
            $newProcess.Dispose()
            continue
        }

        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
        $parentPid = [int]$process.ParentProcessId
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentPid" -ErrorAction SilentlyContinue

        [ordered]@{
            timestamp = (Get-Date).ToString('o')
            processId = $pidValue
            parentProcessId = $parentPid
            name = [string]$newProcess.ProcessName + '.exe'
            mainWindowTitle = [string]$newProcess.MainWindowTitle
            hasWindow = ($newProcess.MainWindowHandle -ne [IntPtr]::Zero)
            commandLine = [string]$process.CommandLine
            executablePath = [string]$process.ExecutablePath
            parentName = [string]$parent.Name
            parentCommandLine = [string]$parent.CommandLine
        } | ConvertTo-Json -Compress | Add-Content -LiteralPath $logPath -Encoding UTF8

        $newProcess.Dispose()
    }
    Start-Sleep -Milliseconds 10
}
