<#
.SYNOPSIS
    Migrate production data (analytics.db, valheatmap.db, raw archives) from Fly.io to Hetzner.

.DESCRIPTION
    Safely checkpoints SQLite WAL files on Fly.io, temporarily pauses the crawler,
    packages the database (~2.4 GB uncompressed) into a compressed tarball,
    downloads it, and optionally uploads and extracts it directly onto your Hetzner server.

.PARAMETER App
    Fly.io app name. Default: valheatmap.

.PARAMETER HetznerHost
    Target Hetzner server SSH login (e.g., "root@123.45.67.89" or "ubuntu@123.45.67.89").
    If omitted, the archive will be saved locally in .\data\ with instructions to upload later.

.PARAMETER HetznerDataDir
    Path to the data directory on Hetzner. Default: /opt/valheatmap/data.

.PARAMETER SkipRaw
    Skip migrating the raw/ payloads directory (~1.8 GB).
    Transfers only analytics.db and valheatmap.db (~2.3 GB uncompressed, ~350 MB compressed).

.EXAMPLE
    # Download backup locally to .\data\valheatmap-migration.tar.gz
    .\deploy\migrate-from-fly.ps1

.EXAMPLE
    # Directly migrate from Fly.io to a Hetzner server
    .\deploy\migrate-from-fly.ps1 -HetznerHost root@192.0.2.1
#>

[CmdletBinding()]
param(
    [string]$App = "valheatmap",
    [string]$HetznerHost = "",
    [string]$HetznerDataDir = "/opt/valheatmap/data",
    [switch]$SkipRaw
)

$ErrorActionPreference = "Stop"

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "     ValHeatMap Migration: Fly.io -> Hetzner Cloud       " -ForegroundColor Green
Write-Host "==========================================================" -ForegroundColor Green

# 1. Verify flyctl is available
if (-not (Get-Command fly -ErrorAction SilentlyContinue)) {
    throw "fly CLI not found. Please install flyctl or run from a shell where fly is in PATH."
}

# Helper to execute remote fly commands without crashing on stderr or exit 1 console quirk
function Invoke-FlyRemote {
    param([string]$Script, [string]$Desc = "Fly command")
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Script))
        $out = fly ssh console --app $App -C "python3 -c `"import base64; exec(base64.b64decode('$encoded').decode())`"" 2>&1
        return ($out | ForEach-Object { "$_" }) -join "`n"
    } finally {
        $ErrorActionPreference = $prev
    }
}

# 2. Check current status
Step "Checking Fly.io instance status"
$status = fly status --app $App 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "Could not connect to Fly app '$App':`n$status"
}
Write-Host "Connected to Fly.io app '$App'." -ForegroundColor Gray

# 3. Checkpoint SQLite WAL databases
Step "Flushing SQLite WAL logs into database files"
$checkpointScript = @'
import sqlite3
for db in ("/data/analytics.db", "/data/valheatmap.db"):
    try:
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        res = cur.fetchone()
        conn.close()
        print(f"Checkpointed {db}: {res}")
    except Exception as e:
        print(f"Error checkpointing {db}: {e}")
'@
$cpResult = Invoke-FlyRemote -Script $checkpointScript -Desc "checkpoint"
Write-Host $cpResult -ForegroundColor Gray

# 4. Pause the crawler on Fly to guarantee no writes during tar
Step "Pausing Fly.io crawler during export"
fly secrets set VALHEATMAP_CRAWLER=0 --app $App 2>&1 | Out-Null
Write-Host "Fly.io crawler paused." -ForegroundColor Gray
Start-Sleep -Seconds 3

# 5. Create compressed archive on Fly
Step "Creating compressed migration archive on Fly.io volume"
$includeItems = "analytics.db valheatmap.db"
if (-not $SkipRaw) {
    $includeItems += " raw"
}

$tarScript = @"
import os, subprocess
cmd = "cd /data && tar -czf /data/migration.tar.gz $includeItems"
print("Running:", cmd)
res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
print("Return code:", res.returncode)
if os.path.exists("/data/migration.tar.gz"):
    size_mb = os.path.getsize("/data/migration.tar.gz") / 1e6
    print(f"Archive created: {size_mb:.1f} MB")
else:
    print("Archive creation failed:", res.stderr)
"@
$tarResult = Invoke-FlyRemote -Script $tarScript -Desc "tar archive"
Write-Host $tarResult -ForegroundColor Gray

# 6. Download the archive to local machine
$repo = Split-Path -Parent $PSScriptRoot
$localDataDir = Join-Path $repo "data"
if (-not (Test-Path $localDataDir)) {
    New-Item -ItemType Directory -Path $localDataDir -Force | Out-Null
}
$localArchive = Join-Path $localDataDir "valheatmap-migration.tar.gz"

Step "Downloading migration archive from Fly.io volume"
Write-Host "Destination: $localArchive" -ForegroundColor Gray
if (Test-Path $localArchive) {
    Remove-Item $localArchive -Force
}

fly ssh sftp get /data/migration.tar.gz $localArchive --app $App 2>&1 | Out-Host

if (-not (Test-Path $localArchive) -or (Get-Item $localArchive).Length -eq 0) {
    throw "Failed to download migration archive from Fly.io."
}
$archSizeMb = [math]::Round((Get-Item $localArchive).Length / 1MB, 1)
Write-Host "Downloaded archive successfully: $archSizeMb MB" -ForegroundColor Green

# 7. Clean up archive on Fly
Step "Cleaning up archive on Fly.io to free disk space"
Invoke-FlyRemote -Script "import os; os.remove('/data/migration.tar.gz') if os.path.exists('/data/migration.tar.gz') else None" | Out-Null

# 8. Transfer to Hetzner (if HetznerHost provided)
if ($HetznerHost) {
    Step "Uploading archive to Hetzner ($HetznerHost)"
    Write-Host "Creating $HetznerDataDir on remote host..." -ForegroundColor Gray
    ssh $HetznerHost "mkdir -p '$HetznerDataDir'"
    
    Write-Host "Streaming $archSizeMb MB to $HetznerHost..." -ForegroundColor Gray
    scp $localArchive "${HetznerHost}:${HetznerDataDir}/valheatmap-migration.tar.gz"
    
    Step "Unpacking on Hetzner host"
    ssh $HetznerHost "cd '$HetznerDataDir' && tar -xzf valheatmap-migration.tar.gz && rm -f valheatmap-migration.tar.gz && ls -lah '$HetznerDataDir'"
    
    Write-Host "`nData successfully transferred and unpacked on Hetzner!" -ForegroundColor Green
} else {
    Write-Host "`n==========================================================" -ForegroundColor Yellow
    Write-Host " Archive saved locally at: $localArchive" -ForegroundColor Yellow
    Write-Host " When your Hetzner server is ready, run:" -ForegroundColor Cyan
    Write-Host "   scp `"$localArchive`" root@<HETZNER_IP>:$HetznerDataDir/" -ForegroundColor White
    Write-Host "   ssh root@<HETZNER_IP> `"cd $HetznerDataDir && tar -xzf valheatmap-migration.tar.gz && rm valheatmap-migration.tar.gz`"" -ForegroundColor White
    Write-Host "==========================================================" -ForegroundColor Yellow
}

Write-Host "`nMigration script finished!" -ForegroundColor Green
