#requires -Version 5.1
<#
  OKX 永续合约量化交易系统 —— 一键启动
  编码：UTF-8 with BOM（无 BOM 时 PS 5.1 按 GBK 读，本文件会语法崩）
#>
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    Write-Host '[X] 找不到 python，请先安装 Python 3.9+' -ForegroundColor Red
    exit 1
}

function Show-Menu {
    Clear-Host
    Write-Host ''
    Write-Host '  ╔══════════════════════════════════════════════════════════╗' -ForegroundColor Magenta
    Write-Host '  ║        OKX 永续合约量化交易系统                           ║' -ForegroundColor Magenta
    Write-Host '  ╚══════════════════════════════════════════════════════════╝' -ForegroundColor Magenta
    Write-Host ''
    Write-Host '   [1] 体检          网络 / 合约 / 凭据 / 余额' -ForegroundColor Cyan
    Write-Host '   [2] 回测          历史数据验证策略' -ForegroundColor Cyan
    Write-Host '   [3] 策略对比      三策略同段行情 PK' -ForegroundColor Cyan
    Write-Host '   [4] 参数寻优      网格搜索最优参数' -ForegroundColor Cyan
    Write-Host '   [5] 模拟盘        真实行情 + 虚拟资金（安全）' -ForegroundColor Green
    Write-Host '   [6] 实盘 dry-run  真实账户 + 只算不下单' -ForegroundColor Yellow
    Write-Host '   [7] 实盘真实下单  ★★ 有真金白银风险 ★★' -ForegroundColor Red
    Write-Host '   [8] 查看状态      模拟盘账本 / 风控熔断' -ForegroundColor Cyan
    Write-Host '   [9] 解除熔断      手动 reset-halt' -ForegroundColor Cyan
    Write-Host '   [0] 退出' -ForegroundColor DarkGray
    Write-Host ''
}

function Invoke-Py {
    param([string[]]$PyArgs)
    Write-Host ''
    & $Python run.py @PyArgs
    Write-Host ''
    Read-Host '按回车返回菜单'
}

while ($true) {
    Show-Menu
    $choice = Read-Host '  选择'

    switch ($choice) {
        '1' { Invoke-Py @('doctor') }
        '2' {
            $bar = Read-Host '  周期 (15m/1H/4H，直接回车用配置默认)'
            $bars = Read-Host '  K线根数 (回车用 1500)'
            $args = @('backtest')
            if ($bar) { $args += @('--bar', $bar) }
            if ($bars) { $args += @('--bars', $bars) }
            Invoke-Py $args
        }
        '3' {
            $bar = Read-Host '  周期 (15m/1H/4H，回车用配置默认)'
            $args = @('compare')
            if ($bar) { $args += @('--bar', $bar) }
            Invoke-Py $args
        }
        '4' {
            $bar = Read-Host '  周期 (15m/1H/4H，回车用配置默认)'
            $args = @('tune', '--strategy', 'trend')
            if ($bar) { $args += @('--bar', $bar) }
            Invoke-Py $args
        }
        '5' { Invoke-Py @('paper') }
        '6' { Invoke-Py @('live') }
        '7' {
            Write-Host ''
            Write-Host '  ★ 警告：这是真实下单，会动真钱。' -ForegroundColor Red
            $c = Read-Host '  确认请输入 YES'
            if ($c -eq 'YES') { Invoke-Py @('live', '--real') }
            else { Write-Host '  已取消' -ForegroundColor DarkGray; Start-Sleep 1 }
        }
        '8' { Invoke-Py @('status') }
        '9' { Invoke-Py @('reset-halt') }
        '0' { break }
        default { }
    }
    if ($choice -eq '0') { break }
}
