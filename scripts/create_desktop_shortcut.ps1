$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DesktopDir = [Environment]::GetFolderPath("Desktop")

$PythonW = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) {
    $PythonW = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path $PythonW)) {
    throw "Interpretador Python do .venv não encontrado em $RepoRoot\.venv\Scripts"
}

$ChromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$IconPath = "$env:SystemRoot\System32\shell32.dll"
$IconIdx = 13
foreach ($c in $ChromeCandidates) {
    if ($c -and (Test-Path $c)) {
        $IconPath = $c
        $IconIdx = 0
        break
    }
}

$ShortcutName = "Est$([char]0x00E1)gio Radar PT.lnk"
$ShortcutPath = Join-Path $DesktopDir $ShortcutName

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PythonW
$Shortcut.Arguments = "-m estagio_radar app"
$Shortcut.WorkingDirectory = $RepoRoot
$Shortcut.IconLocation = "$IconPath,$IconIdx"
$Shortcut.Description = "Estagio Radar PT - Dashboard + Chrome Dedicado (isolado dos teus outros perfis)"
$Shortcut.Save()

Write-Output "Atalho criado com sucesso: $ShortcutPath"
