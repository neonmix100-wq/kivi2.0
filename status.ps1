$root = "D:\Kivi"
function Chk($label, $cond) {
  if ($cond) { Write-Host "[OK]      $label" -ForegroundColor Green }
  else       { Write-Host "[MISSING] $label" -ForegroundColor Red }
}
Write-Host "=== KIVI STATUS ===" -ForegroundColor Cyan
Chk "D:\Kivi exists" (Test-Path $root)

$expected = @("kivi","kivi\audio","kivi\stt","kivi\wake","kivi\speaker","kivi\brain",
"kivi\tts","kivi\lipsync","kivi\skills","kivi\memory","kivi\safety","avatar",
"extension","workspace","tests","data\logs","data\models","data\tmp")
$miss = @(); foreach ($d in $expected) { if (-not (Test-Path (Join-Path $root $d))) { $miss += $d } }
Chk "18 project folders" ($miss.Count -eq 0)
if ($miss.Count -gt 0) { Write-Host "   missing: $($miss -join ', ')" }

foreach ($f in @("AGENTS.md","config.yaml","requirements.txt",".gitignore",".env",
".env.example","tests\smoke_env.py","tests\check_api.py","tests\list_models.py",".roorules")) {
  Chk $f (Test-Path (Join-Path $root $f))
}
Chk "git repo (.git)" (Test-Path "$root\.git")

$py = "$root\.venv\Scripts\python.exe"
Chk "venv python.exe" (Test-Path $py)
if (Test-Path $py) {
  $v = & $py -c "import requests;print(requests.__version__)" 2>$null
  Chk "requests installed in venv" ($null -ne $v)
  if ($v) { Write-Host "   requests $v" }
}

if (Test-Path "$root\.env") {
  Write-Host "--- .env ---" -ForegroundColor Cyan
  foreach ($line in Get-Content "$root\.env") {
    if ($line -match "^\s*([A-Za-z_]+)\s*=(.*)$") {
      $n = $matches[1]; $v = $matches[2].Trim()
      if ($n -like "*KEY*") { Chk "$n (length $($v.Length))" ($v.Length -gt 0) }
      else { Chk "$n = $v" ($v.Length -gt 0) }
    }
  }
}
Write-Host "--- python launcher ---" -ForegroundColor Cyan
py --list
Write-Host "=== END ===" -ForegroundColor Cyan