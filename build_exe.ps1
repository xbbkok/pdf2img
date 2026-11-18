<#
打包成 Windows 独立 exe（PyInstaller）

用法：
    powershell -ExecutionPolicy Bypass -File build_exe.ps1                    # 目录版（推荐）
    powershell -ExecutionPolicy Bypass -File build_exe.ps1 -Mode onefile     # 单文件版
    powershell -ExecutionPolicy Bypass -File build_exe.ps1 -Only cli

产物：
    目录版  dist\PDF转图片\PDF转图片.exe        （双击即用，可把 PDF 拖到 exe 上）
            dist\pdf2img-cli\pdf2img-cli.exe    （命令行）
    单文件版 dist\PDF转图片-单文件.exe
            dist\pdf2img-cli-单文件.exe

两种模式的区别：
    目录版：启动快（约 0.3 秒），不需要运行时解包，杀软误报少；分发时要整个文件夹一起拷。
    单文件：只有一个文件方便发送，但每次启动会解包到临时目录（慢 1-3 秒），
            部分安全软件会拦截解包行为。
#>
param(
    [ValidateSet("onedir", "onefile")]
    [string]$Mode = "onedir",
    [ValidateSet("all", "gui", "cli")]
    [string]$Only = "all"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$env:PYTHONPATH = Join-Path $root "build\pyi"
$env:TMP = Join-Path $root ".tmp"
$env:TEMP = $env:TMP
New-Item -ItemType Directory -Force -Path $env:TMP | Out-Null

$modeFlag = if ($Mode -eq "onefile") { "--onefile" } else { "--onedir" }
# 单文件版输出到 portable\（便于直接分发），目录版输出到 dist\
$outRoot = if ($Mode -eq "onefile") { "portable" } else { "dist" }

$common = @(
    "--noconfirm", "--clean", $modeFlag,
    "--icon", (Join-Path $root "assets\pdf2img.ico"),
    "--version-file", (Join-Path $root "assets\version_info.txt"),
    "--distpath", (Join-Path $root $outRoot),
    "--workpath", (Join-Path $root "build\work"),
    "--specpath", (Join-Path $root "build"),
    "--exclude-module", "numpy",
    "--exclude-module", "pandas",
    "--exclude-module", "matplotlib",
    "--exclude-module", "scipy",
    "--exclude-module", "PyQt5",
    "--exclude-module", "PyQt6",
    "--exclude-module", "PySide2",
    "--exclude-module", "PySide6",
    "--exclude-module", "IPython",
    "--exclude-module", "jupyter",
    "--exclude-module", "notebook",
    "--exclude-module", "pytest",
    "--exclude-module", "sphinx"
)

function Build-One {
    param([string]$Name, [string]$Entry, [string]$Subsystem)
    Write-Host ""
    Write-Host "===== 构建 $Name ($Mode / $Subsystem) =====" -ForegroundColor Cyan
    $pyArgs = @("-m", "PyInstaller") + $common +
              @("--name", $Name, "--$Subsystem", (Join-Path $root $Entry))
    & python @pyArgs
    if ($LASTEXITCODE -ne 0) { throw "构建 $Name 失败（exit $LASTEXITCODE）" }

    $exe = Join-Path $root "$outRoot\$Name.exe"
    if (-not (Test-Path $exe)) { $exe = Join-Path $root "$outRoot\$Name\$Name.exe" }
    if (-not (Test-Path $exe)) { throw "未找到产物：$outRoot\$Name.exe" }

    if ($Mode -eq "onefile") {
        $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    } else {
        $mb = [math]::Round((Get-ChildItem (Split-Path $exe) -Recurse -File |
                             Measure-Object Length -Sum).Sum / 1MB, 1)
    }
    Write-Host ("OK {0}.exe  合计 {1} MB" -f $Name, $mb) -ForegroundColor Green
}

if ($Only -eq "all" -or $Only -eq "gui") {
    Build-One "PDF转图片" "exe_gui.py" "windowed"
}
if ($Only -eq "all" -or $Only -eq "cli") {
    Build-One "pdf2img-cli" "exe_cli.py" "console"
}

Write-Host ""
Write-Host "完毕。产物目录：$root\$outRoot" -ForegroundColor Green
Get-ChildItem (Join-Path $root $outRoot) | Select-Object Name
