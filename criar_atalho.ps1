$ErrorActionPreference = "Stop"
$folder = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$linkPath = Join-Path $desktop "Gerador de Horários Escolares.lnk"
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = Join-Path $folder "iniciar_gerador.bat"
$shortcut.WorkingDirectory = $folder
$shortcut.Description = "Abre o Gerador de Horários Escolares"
$shortcut.IconLocation = "$env:SystemRoot\System32\SHELL32.dll,13"
$shortcut.Save()
Write-Host "Atalho criado na Área de Trabalho: $linkPath"
