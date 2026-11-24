param(
    [int]$IntervalMinutes = 5,
    [int]$DeepIntervalMinutes = 60,
    [int]$UiPort = 8765
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PythonW = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) {
    throw "Executavel '$PythonW' nao encontrado. Cria o .venv com Python 3.12 primeiro."
}

$DataDir = Join-Path $RepoRoot "data"
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

$Arguments = "-m estagio_radar watch --sniper-interval $IntervalMinutes --deep-interval $DeepIntervalMinutes --notify --with-ui --ui-port $UiPort --log-file data/radar.log"
$TaskName = "EstagioRadarPT_Daemon"

# Limpar tarefas antigas (que usavam cmd.exe) se existirem
foreach ($LegacyTask in @("EstagioRadarPT_Sniper", "EstagioRadarPT_DeepHourly")) {
    try {
        $existing = Get-ScheduledTask -TaskName $LegacyTask -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $LegacyTask -Confirm:$false -ErrorAction SilentlyContinue
        }
    } catch {
        # Ignorar se não existir ou sem acesso
    }
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$TaskRegistered = $false

try {
    $Action = New-ScheduledTaskAction -Execute $PythonW -Argument $Arguments -WorkingDirectory $RepoRoot
    $TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
    $TriggerWatchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 15) `
        -RepetitionDuration (New-TimeSpan -Days 365)
    $Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger @($TriggerLogon, $TriggerWatchdog) `
        -Principal $Principal `
        -Settings $Settings `
        -Description "Estagio Radar PT v4.0: Sniper silencioso (${IntervalMinutes}m), Deep sweep (${DeepIntervalMinutes}m) e Dashboard (:$UiPort)." `
        -Force `
        -ErrorAction Stop | Out-Null

    $TaskRegistered = $true
    Write-Host "Tarefa silenciosa '$TaskName' registada no Task Scheduler ($IntervalMinutes min Sniper / $DeepIntervalMinutes min Deep / UI :$UiPort)."
} catch {
    Write-Warning "Task Scheduler nao disponivel ($($_.Exception.Message)). A instalar atalho silencioso na pasta Startup..."
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = Join-Path $StartupDir "EstagioRadarPT_Daemon.lnk"
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $PythonW
    $Shortcut.Arguments = $Arguments
    $Shortcut.WorkingDirectory = $RepoRoot
    $Shortcut.WindowStyle = 7
    $Shortcut.Description = "Estágio Radar PT v4.0 Daemon Silencioso"
    $Shortcut.Save()
    Write-Host "Atalho silencioso criado em '$ShortcutPath'."
}

# Iniciar o daemon imediatamente em segundo plano (sem janelas)
if ($TaskRegistered) {
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "Daemon '$TaskName' iniciado via Task Scheduler."
    } catch {
        Start-Process -FilePath $PythonW -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden
        Write-Host "Daemon iniciado em segundo plano com pythonw.exe."
    }
} else {
    Start-Process -FilePath $PythonW -ArgumentList $Arguments -WorkingDirectory $RepoRoot -WindowStyle Hidden
    Write-Host "Daemon iniciado em segundo plano com pythonw.exe."
}
