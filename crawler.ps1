# Runs the ValHeatMap crawler with a tray icon, showing log output.
# Use crawler.vbs instead if you want it silent in the background.
$ErrorActionPreference = 'Stop'
Push-Location "$PSScriptRoot\backend"
python -m pip install -q -r requirements.txt
python -m app.tray --verbose @args
Pop-Location
