$root = "D:\Kivi"

$dirs = @(
  "kivi", "kivi\audio", "kivi\stt", "kivi\wake", "kivi\speaker", "kivi\brain",
  "kivi\tts", "kivi\lipsync", "kivi\skills", "kivi\memory", "kivi\safety",
  "avatar", "extension", "workspace", "tests",
  "data\logs", "data\models", "data\tmp"
)

foreach ($d in $dirs) {
  $p = Join-Path $root $d
  New-Item -ItemType Directory -Force -Path $p | Out-Null
  if ($d -like "kivi*") {
    $init = Join-Path $p "__init__.py"
    if (-not (Test-Path $init)) { New-Item -ItemType File -Path $init | Out-Null }
  }
}

$utf8 = New-Object System.Text.UTF8Encoding($false)

$gi = @(".venv/", ".env", "data/", "__pycache__/", "*.pyc", "*.wav", "node_modules/") -join "`r`n"

$envex = @(
  "LLM_BASE_URL=https://polza.ai/api/v1",
  "LLM_API_KEY=",
  "LLM_MODEL_CHEAP=",
  "LLM_MODEL_CODER=",
  "LLM_MODEL_VISION=",
  "TTS_MODEL="
) -join "`r`n"

$req = @(
  "sounddevice", "soundfile", "numpy", "onnxruntime", "requests", "loguru",
  "pydantic", "pydantic-settings", "pyyaml", "python-dotenv", "psutil", "nvidia-ml-py"
) -join "`r`n"

[System.IO.File]::WriteAllText((Join-Path $root ".gitignore"), $gi, $utf8)
[System.IO.File]::WriteAllText((Join-Path $root ".env.example"), $envex, $utf8)
[System.IO.File]::WriteAllText((Join-Path $root "requirements.txt"), $req, $utf8)

Write-Host ""
Write-Host "OK. Folders created:" -ForegroundColor Green
Get-ChildItem -Path $root -Recurse -Directory | ForEach-Object { $_.FullName }
Write-Host ""
Write-Host "Files:" -ForegroundColor Green
Get-ChildItem -Path $root -File -Force | ForEach-Object { $_.Name }