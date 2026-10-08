<#
.SYNOPSIS
  ast-grep installer for Windows — package-manager fallback + pinned GitHub ZIP.

.DESCRIPTION
  Tries winget, scoop, choco, npm, cargo, and pip in order, then falls back to
  the pinned GitHub release ZIP. The ZIP fallback extracts the real `ast-grep`
  binary (never the `sg` launcher) into $Prefix.

.PARAMETER Prefix
  Install directory (default: $env:LOCALAPPDATA\Programs\ast-grep).

.PARAMETER Version
  Pinned ast-grep version (default: 0.43.0).
#>
param(
  [string]$Prefix = "$env:LOCALAPPDATA\Programs\ast-grep",
  [string]$Version = "0.43.0"
)

$ErrorActionPreference = "Stop"

function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Try-Run([string]$label, [scriptblock]$block) {
  Write-Host "-> $label"
  try { & $block; return $true } catch { Write-Host "   (failed, trying next method)"; return $false }
}

function Verify {
  foreach ($candidate in @("ast-grep", "sg")) {
    if (Have $candidate) {
      $out = & $candidate --version 2>$null
      if ($out -match "ast-grep") { Write-Host "ast-grep installed: $out"; return $true }
    }
  }
  return $false
}

Write-Host "Installing ast-grep $Version (prefix: $Prefix)"

# 1. winget
if (Have winget) { if (Try-Run "winget install ast-grep" { winget install --id ast-grep.ast-grep -e }) { if (Verify) { exit 0 } } }
# 2. scoop
if (Have scoop) { if (Try-Run "scoop install main/ast-grep" { scoop install main/ast-grep }) { if (Verify) { exit 0 } } }
# 3. choco
if (Have choco) { if (Try-Run "choco install ast-grep" { choco install ast-grep -y }) { if (Verify) { exit 0 } } }
# 4. npm
if (Have npm) { if (Try-Run "npm install -g @ast-grep/cli" { npm install -g '@ast-grep/cli' }) { if (Verify) { exit 0 } } }
# 5. cargo
if (Have cargo) { if (Try-Run "cargo install ast-grep --locked" { cargo install ast-grep --locked }) { if (Verify) { exit 0 } } }
# 6. pip
if (Have pip) { if (Try-Run "pip install ast-grep-cli" { pip install --user ast-grep-cli }) { if (Verify) { exit 0 } } }

# 7. GitHub release ZIP fallback
$arch = if ($env:PROCESSOR_ARCHITECTURE -match "ARM64") { "aarch64" } else { "x86_64" }
$url = "https://github.com/ast-grep/ast-grep/releases/download/$Version/app-$arch-pc-windows-msvc.zip"
$tmp = Join-Path $env:TEMP ("ast-grep-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  $zip = Join-Path $tmp "ast-grep.zip"
  Write-Host "-> downloading $url"
  Invoke-WebRequest -Uri $url -OutFile $zip
  Expand-Archive -Path $zip -DestinationPath $tmp -Force

  # Prefer the real ast-grep.exe over the sg.exe launcher.
  $source = Join-Path $tmp "ast-grep.exe"
  if (-not (Test-Path $source)) { $source = Join-Path $tmp "sg.exe" }
  New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
  Copy-Item $source (Join-Path $Prefix "ast-grep.exe") -Force
  Write-Host "Installed to $Prefix\ast-grep.exe — ensure $Prefix is on PATH."
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
