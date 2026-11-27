# 用 Advanced Installer 命令行构建 Windows 安装包

这套流程在 CodecFoundry 1.4.1 上用 Advanced Installer 22.4 验证过。只要已有 `.aip` 项目和编译后的程序，就可以从 PowerShell 构建安装包，无需打开图形界面。

## 找到命令行工具

先用 `Get-Command AdvancedInstaller.com` 检查 PATH。未找到时，在 Windows 的已安装应用中找到 Advanced Installer，确认安装目录，再找其中的 `bin\x86\AdvancedInstaller.com`。本机安装在 D 盘，运行时使用绝对路径。

```powershell
$installerHome = 'D:\Program Files (x86)\Caphyon\Advanced Installer 22.4'
$cli = Join-Path $installerHome 'bin\x86\AdvancedInstaller.com'
& $cli /help build
```

## 准备新版本

1. 用项目原有工具编译程序，把应用版本、EXE 的 Windows 产品版本和安装项目的产品版本设为同一发行版本。CodecFoundry 使用 PyInstaller 的目录模式，保留 `dist\CodecFoundry` 下的 EXE 和完整 `_internal`。
2. 从上一版复制 `.aip` 及它使用的图标、背景图等项目素材。保留相对目录关系，例如 `AIP\CodecFoundry.aip` 和 `dist\CodecFoundry\CodecFoundry.exe` 并列放在同一个版本目录下。干净的构建目录不需要上一版的安装包或 `*-cache`。
3. 更新 AIP 的产品版本。沿用现有的升级策略。CodecFoundry 的项目配置使用 MSI 主升级，新版使用新的 `ProductCode`，沿用 `UpgradeCode` 和既有组件标识。
4. 核对 AIP 的文件清单和新版构建输出。CodecFoundry 的模板逐项列出文件，本次需要补进 4 个 DLL。同步目录项目可以用 `/edit <项目路径> /RefreshSync` 刷新。图标等资源也要逐一核对，本次把指向 `v1.0.0` 目录的快捷方式图标改为当前 AIP 自带的图标。

## 在 Temp 下构建并验证

把 `AIP` 和 `dist` 一起复制到项目自己的 `Temp\<构建目录>`。以下命令在项目根目录运行，将示例目录、项目文件名及输出名换成当前项目的实际值。

```powershell
$stage = (Resolve-Path -LiteralPath '.\Temp\installer-build').Path
$project = Join-Path $stage 'AIP\CodecFoundry.aip'
$setup = Join-Path $stage 'AIP\CodecFoundry_setup.exe'

& $cli /build $project
if ($LASTEXITCODE -ne 0) {
    throw "Advanced Installer 构建失败，退出码为 $LASTEXITCODE"
}

$package = Get-Item -LiteralPath $setup -ErrorAction Stop
$package.VersionInfo | Select-Object ProductVersion, FileVersion
Get-FileHash -LiteralPath $package.FullName -Algorithm SHA256
```

需要忽略缓存重建时，用 `/rebuild` 替换 `/build`。本次受限会话能创建 CAB 和 MSI，但在写入 EXE 的 Vista UAC 执行级别时返回“参数错误”。确认项目资源路径和输出目录正确后，在有权限的普通 Windows 终端或经批准的执行环境中对同一临时副本重试，构建和 MSI 验证均通过。

核对安装包版本和 SHA-256，再实测安装包。试跑通过后，把同一份安装包移到 `Release\v<版本号>\AIP\`，确认哈希与试跑件一致。CodecFoundry 1.4.1 使用这个文件作为双平台的 Release 附件。安装包在两个平台都可下载后再更新自动更新清单。

[Advanced Installer 命令行文档](https://docs.advancedinstaller.com/command-line.html)
