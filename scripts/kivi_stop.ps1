# Корректная остановка Kivi: только процессы python с "kivi.main" и ffplay.
# Посторонние python (VS Code, Cline и т.п.) не трогаем.
# venv-шим порождает дочерний интерпретатор, поэтому делаем несколько проходов.

$ErrorActionPreference = 'Continue'
$stopped = New-Object System.Collections.Generic.List[string]

function Get-KiviProcesses {
    @(
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.CommandLine -match 'kivi\.main' }
    )
}

for ($pass = 1; $pass -le 3; $pass++) {
    $kivi = Get-KiviProcesses
    if ($kivi.Count -eq 0) { break }

    foreach ($proc in $kivi) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            $label = "python.exe pid=$($proc.ProcessId) (kivi.main)"
            if (-not $stopped.Contains($label)) { $stopped.Add($label) }
        }
        catch {
            Write-Host "не удалось остановить pid=$($proc.ProcessId): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    Start-Sleep -Milliseconds 400
}

$players = @(Get-Process -Name ffplay -ErrorAction SilentlyContinue)
foreach ($player in $players) {
    try {
        Stop-Process -Id $player.Id -Force -ErrorAction Stop
        $stopped.Add("ffplay pid=$($player.Id)")
    }
    catch {
        Write-Host "не удалось остановить ffplay pid=$($player.Id): $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if ($stopped.Count -eq 0) {
    Write-Host "Киви не запущена - останавливать нечего." -ForegroundColor Green
}
else {
    Write-Host "Остановлено:" -ForegroundColor Green
    foreach ($line in $stopped) { Write-Host "  $line" }
}
