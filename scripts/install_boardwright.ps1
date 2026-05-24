param(
    [ValidateSet("User", "Project")]
    [string]$Scope = "User",
    [switch]$NoTui,
    [switch]$NoPathUpdate,
    [switch]$InstallProfileCommand,
    [switch]$Run
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$extra = if ($NoTui) { "" } else { ".[tui]" }

if ($Scope -eq "Project") {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        python -m venv .venv
    }

    if ($NoTui) {
        & $venvPython -m pip install -e .
    } else {
        & $venvPython -m pip install -e $extra
    }

    $launcher = Join-Path $RepoRoot "boardwright.ps1"
    $launcherText = @'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$RepoRoot\.venv\Scripts\boardwright.exe" @args
'@
    Set-Content -LiteralPath $launcher -Value $launcherText -Encoding UTF8

    Write-Host ""
    Write-Host "Boardwright installed into this project:"
    Write-Host "  $RepoRoot\.venv"
    Write-Host ""
    Write-Host "Run from this project with:"
    Write-Host "  .\boardwright.ps1"
} else {
    if ($NoTui) {
        python -m pip install -e .
    } else {
        python -m pip install -e $extra
    }

    $ScriptsDir = python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
    $pathParts = $env:Path -split ';' | Where-Object { $_ }
    $scriptsOnCurrentPath = $pathParts -contains $ScriptsDir

    if (-not $scriptsOnCurrentPath) {
        $env:Path = "$env:Path;$ScriptsDir"
    }

    if (-not $NoPathUpdate) {
        $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
        $userPathParts = $userPath -split ';' | Where-Object { $_ }
        if ($userPathParts -notcontains $ScriptsDir) {
            $newUserPath = if ([string]::IsNullOrWhiteSpace($userPath)) {
                $ScriptsDir
            } else {
                "$userPath;$ScriptsDir"
            }
            [System.Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
            Write-Host ""
            Write-Host "Added Python Scripts directory to your user PATH:"
            Write-Host "  $ScriptsDir"
            Write-Host "Open a new terminal, or keep using this installer shell which has PATH refreshed."
        }
    }

    Write-Host ""
    Write-Host "Boardwright installed as a user command. Run it from any Boardwright project with:"
    Write-Host "  boardwright"
}

if ($InstallProfileCommand) {
    $profileDir = Split-Path -Parent $PROFILE.CurrentUserCurrentHost
    if (-not (Test-Path $profileDir)) {
        New-Item -ItemType Directory -Force $profileDir | Out-Null
    }
    if (-not (Test-Path $PROFILE.CurrentUserCurrentHost)) {
        New-Item -ItemType File -Force $PROFILE.CurrentUserCurrentHost | Out-Null
    }

    $profileText = Get-Content -LiteralPath $PROFILE.CurrentUserCurrentHost -Raw
    $marker = "# Boardwright project-aware launcher"
    if ($profileText -notlike "*$marker*") {
        $functionText = @'

# Boardwright project-aware launcher
function boardwright {
    $dir = (Get-Location).Path
    while ($dir) {
        $localExe = Join-Path $dir ".venv\Scripts\boardwright.exe"
        if (Test-Path $localExe) {
            & $localExe @args
            return
        }

        $localScript = Join-Path $dir "boardwright.ps1"
        if (Test-Path $localScript) {
            & $localScript @args
            return
        }

        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) {
            break
        }
        $dir = $parent
    }

    $global = Get-Command boardwright.exe -ErrorAction SilentlyContinue
    if ($global) {
        & $global.Source @args
        return
    }

    Write-Error "No project-local or global Boardwright install found."
}
'@
        Add-Content -LiteralPath $PROFILE.CurrentUserCurrentHost -Value $functionText
        Write-Host ""
        Write-Host "Added a project-aware boardwright function to:"
        Write-Host "  $($PROFILE.CurrentUserCurrentHost)"
        Write-Host "Open a new terminal, or dot-source your profile to use it now:"
        Write-Host "  . `$PROFILE"
    }
}

if ($Run) {
    if ($Scope -eq "Project") {
        & (Join-Path $RepoRoot "boardwright.ps1")
    } else {
        boardwright
    }
}
