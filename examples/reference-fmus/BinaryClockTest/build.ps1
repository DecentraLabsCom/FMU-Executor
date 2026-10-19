[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$buildDir = Join-Path $projectDir '.build-windows'
$sourceFile = Join-Path $projectDir 'src\BinaryClockTest.c'
$dllPath = Join-Path $buildDir 'BinaryClockTest.dll'

$vsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vcVars = $null
if (Test-Path -LiteralPath $vsWhere) {
    $vsPath = & $vsWhere -latest -property installationPath
    if ($vsPath) {
        $vcVars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
    }
}
if (-not $vcVars -or -not (Test-Path -LiteralPath $vcVars)) {
    throw 'Visual Studio C++ Build Tools with the x64 compiler are required.'
}

New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
$batchFile = Join-Path $buildDir '_compile.bat'
$batch = "@echo off`r`ncall `"$vcVars`" >nul 2>&1`r`nif errorlevel 1 exit /b 1`r`ncl /LD /O2 /W4 /WX /nologo /DWIN32 `"$sourceFile`" /Fe:`"$dllPath`" /Fo:`"$buildDir\BinaryClockTest.obj`""
[System.IO.File]::WriteAllText($batchFile, $batch, [System.Text.Encoding]::ASCII)

cmd /c $batchFile
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $dllPath)) {
    throw 'BinaryClockTest Windows x64 compilation failed.'
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python 3 is required to package the FMU.'
}
& $python.Source (Join-Path $projectDir 'package_fmu.py') --platform windows --binary $dllPath
if ($LASTEXITCODE -ne 0) {
    throw 'BinaryClockTest Windows FMU packaging failed.'
}
