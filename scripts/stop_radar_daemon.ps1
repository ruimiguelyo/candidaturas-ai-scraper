param(
    [switch]$CleanState
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PidPath = Join-Path $RepoRoot "data\radar_watch.pid"
$LockPath = Join-Path $RepoRoot "data\radar_watch.lock"
$TaskName = "EstagioRadarPT_Daemon"

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "Tarefa '$TaskName' parada."
    }
} catch {
    # Ignorar se Task Scheduler não acessível
}

$StoppedPid = $null
if (Test-Path $PidPath) {
    $RawPid = (Get-Content $PidPath -Raw -ErrorAction SilentlyContinue).Trim()
    if ($RawPid -match '^\d+$') {
        $TargetPid = [int]$RawPid
        $Proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        if ($Proc -and $Proc.ProcessName -match '^pythonw?$') {
            Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
            $StoppedPid = $TargetPid
            Write-Host "Processo daemon Estagio Radar (PID $TargetPid) parado."
        } else {
            Write-Host "PID $TargetPid ja nao corresponde a um processo pythonw/python ativo."
        }
    }
} else {
    Write-Host "Nenhum ficheiro PID encontrado em '$PidPath'."
}

if ($CleanState -or $StoppedPid) {
    Remove-Item -Path $PidPath -Force -ErrorAction SilentlyContinue
    if ($CleanState) {
        Remove-Item -Path $LockPath -Force -ErrorAction SilentlyContinue
        Write-Host "Ficheiros de lock e PID removidos."
    }
}
