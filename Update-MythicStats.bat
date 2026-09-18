@echo off
rem Mythic Stat Sheet updater - double-click to download the latest addon
rem and drop it into your World of Warcraft AddOns folder.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:MSU_DIR='%~dp0'; $f=[IO.File]::ReadAllText('%~f0'); $m='#POWER'+'SHELL#'; $i=$f.LastIndexOf($m); Invoke-Expression $f.Substring($i+$m.Length)"
exit /b

#POWERSHELL#
$ErrorActionPreference = 'Stop'
$Url        = 'https://bysonuk.github.io/wcltop10/MythicStats.zip'
$AddonName  = 'MythicStats'
$ConfigDir  = Join-Path $env:LOCALAPPDATA 'MythicStatsUpdater'
$ConfigFile = Join-Path $ConfigDir 'addons-path.txt'

function Write-Head($text) { Write-Host ''; Write-Host $text -ForegroundColor Cyan }

function Test-AddOnsPath($path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $false }
    return (Test-Path -LiteralPath $path -PathType Container)
}

function Find-AddOnsPath {
    # 1. Next to this file, if it was dropped straight into AddOns
    $here = $env:MSU_DIR
    if ($here) { $here = $here.TrimEnd('\') }
    if ($here -and $here.ToLower().EndsWith('interface\addons')) { return $here }

    # 2. Remembered from last time
    if (Test-Path -LiteralPath $ConfigFile) {
        $saved = (Get-Content -LiteralPath $ConfigFile -Raw).Trim()
        if (Test-AddOnsPath $saved) { return $saved }
    }

    # 3. Where Blizzard says the game is
    $keys = @(
        'HKLM:\SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft',
        'HKLM:\SOFTWARE\Blizzard Entertainment\World of Warcraft'
    )
    foreach ($k in $keys) {
        try {
            $install = (Get-ItemProperty -Path $k -ErrorAction Stop).InstallPath
            if ($install) {
                $try = Join-Path $install 'Interface\AddOns'
                if (Test-AddOnsPath $try) { return $try }
                $try = Join-Path (Split-Path -Parent $install.TrimEnd('\')) '_retail_\Interface\AddOns'
                if (Test-AddOnsPath $try) { return $try }
            }
        } catch { }
    }

    # 4. The usual places, on every drive
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem).Name) {
        foreach ($tail in @('World of Warcraft\_retail_\Interface\AddOns',
                            'Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns',
                            'Program Files\World of Warcraft\_retail_\Interface\AddOns',
                            'Games\World of Warcraft\_retail_\Interface\AddOns')) {
            $try = "${drive}:\$tail"
            if (Test-AddOnsPath $try) { return $try }
        }
    }

    # 5. Ask
    Write-Host ''
    Write-Host "I couldn't find your AddOns folder." -ForegroundColor Yellow
    Write-Host 'It usually looks like: C:\Program Files (x86)\World of Warcraft\_retail_\Interface\AddOns'
    $typed = Read-Host 'Paste the full path to your AddOns folder'
    $typed = $typed.Trim('"').Trim()
    if (Test-AddOnsPath $typed) { return $typed }
    return $null
}

try {
    Write-Head 'Mythic Stat Sheet updater'

    $addons = Find-AddOnsPath
    if (-not $addons) {
        Write-Host 'That folder does not exist, so nothing was installed.' -ForegroundColor Red
        Read-Host 'Press Enter to close'
        exit 1
    }
    Write-Host "AddOns folder: $addons"

    Write-Host 'Downloading the latest build...'
    $tempZip = Join-Path $env:TEMP 'MythicStats.zip'
    $tempDir = Join-Path $env:TEMP 'MythicStatsUnzip'
    if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }

    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $Url -OutFile $tempZip -UseBasicParsing
    Expand-Archive -LiteralPath $tempZip -DestinationPath $tempDir -Force

    $source = Join-Path $tempDir $AddonName
    if (-not (Test-Path -LiteralPath $source)) { $source = $tempDir }

    $target = Join-Path $addons $AddonName
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Copy-Item -LiteralPath $source -Destination $target -Recurse -Force

    # Remember where it went, for next time
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Set-Content -LiteralPath $ConfigFile -Value $addons

    $toc = Join-Path $target "$AddonName.toc"
    $when = ''
    if (Test-Path -LiteralPath $toc) {
        $line = (Get-Content -LiteralPath $toc | Where-Object { $_ -like '## Notes:*' } | Select-Object -First 1)
        if ($line) { $when = $line -replace '^## Notes:\s*', '' }
    }

    Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
    Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue

    Write-Host ''
    Write-Host 'Installed.' -ForegroundColor Green
    if ($when) { Write-Host "  $when" }
    Write-Host "  $target"
    Write-Host ''
    Write-Host 'If the game is running, type /reload to pick it up.'
}
catch {
    Write-Host ''
    Write-Host "Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'If it mentions the download, check that the site has published a build yet.'
}

Read-Host 'Press Enter to close'
