# Статус Kivi: запущена ли, pid, время старта, занят ли порт-замок.
# venv-шим python.exe порождает дочерний интерпретатор — это одна Киви,
# поэтому показываем корневой процесс и упоминаем дочерний.

$ErrorActionPreference = 'Continue'

function Test-KiviPort([int]$Port) {
    # Смотрим таблицу TCP, НЕ подключаясь: connect занимал бы очередь сокета.
    $listening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    return ($listening.Count -gt 0)
}

$all = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'kivi\.main' }
)

if ($all.Count -eq 0) {
    Write-Host "Киви не запущена." -ForegroundColor Yellow
    exit 1
}

$pids = @($all | ForEach-Object { $_.ProcessId })
$roots = @($all | Where-Object { $pids -notcontains $_.ParentProcessId })
if ($roots.Count -eq 0) { $roots = $all }

Write-Host "Киви запущена:" -ForegroundColor Green
foreach ($proc in $roots) {
    $started = $proc.CreationDate
    $uptime = (New-TimeSpan -Start $started)
    Write-Host ("  pid={0}  старт={1:yyyy-MM-dd HH:mm:ss}  работает={2:hh\:mm\:ss}" -f $proc.ProcessId, $started, $uptime)
    $children = @($all | Where-Object { $_.ParentProcessId -eq $proc.ProcessId })
    foreach ($child in $children) {
        Write-Host ("    дочерний pid={0} ({1})" -f $child.ProcessId, (Split-Path $child.ExecutablePath -Leaf))
    }
}

$busy = Test-KiviPort 47613
if ($busy) {
    Write-Host "  порт-замок 47613: занят (это Киви)" -ForegroundColor Green
}
else {
    Write-Host "  порт-замок 47613: свободен (процесс завершается или завис)" -ForegroundColor Yellow
}

