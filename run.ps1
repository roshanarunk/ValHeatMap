# Build the frontend and serve the whole app from one FastAPI process.
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot

Push-Location frontend
if (-not (Test-Path node_modules)) { npm install }
npm run build
Pop-Location

Push-Location backend
python -m pip install -q -r requirements.txt
Write-Host "`nValHeatMap running at http://127.0.0.1:8000`n" -ForegroundColor Green
python -m uvicorn app.main:app --port 8000
Pop-Location
Pop-Location
