$ErrorActionPreference = 'Stop'

$exampleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputPath = Join-Path $exampleRoot 'SmartWarehouseCell.fmu'
$workPath = Join-Path $exampleRoot '.build'
$binaryPath = Join-Path $workPath 'binaries\win64'
$resourcePath = Join-Path $workPath 'resources'

python (Join-Path $exampleRoot 'generate_scene.py')
if ($LASTEXITCODE -ne 0) { throw 'GLB scene generation failed.' }
Remove-Item -LiteralPath $workPath -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $binaryPath, $resourcePath -Force | Out-Null
Copy-Item (Join-Path $exampleRoot 'modelDescription.xml') $workPath
Copy-Item (Join-Path $exampleRoot 'smart-warehouse-cell.glb') $resourcePath

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$visualStudio = if (Test-Path -LiteralPath $vswhere) {
  & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
}
if (-not $visualStudio) { $visualStudio = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2019\BuildTools' }
$vcvars = Join-Path $visualStudio 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { throw 'Visual Studio C++ Build Tools (x64) are required to build this FMI 2.0 FMU.' }

$dllPath = Join-Path $binaryPath 'SmartWarehouseCell.dll'
$compilerCommand = 'call "{0}" >nul && cd /d "{1}" && cl /nologo /LD /MT /O2 /W4 /D_CRT_SECURE_NO_WARNINGS /Fo:.build\binaries\win64\ /Fe:.build\binaries\win64\SmartWarehouseCell.dll warehouse.c' -f $vcvars, $exampleRoot
& $env:ComSpec /d /s /c $compilerCommand
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $dllPath)) { throw 'The Smart Warehouse FMI binary could not be compiled.' }
Get-ChildItem -LiteralPath $binaryPath -File | Where-Object { $_.Name -ne 'SmartWarehouseCell.dll' } | Remove-Item -Force
if (Test-Path -LiteralPath $outputPath) { Remove-Item -LiteralPath $outputPath -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($workPath, $outputPath, [System.IO.Compression.CompressionLevel]::Optimal, $false)
Write-Output "Built $outputPath"
