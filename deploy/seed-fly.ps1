<#
.SYNOPSIS
    Seed the Fly volume with the local dataset, instead of re-crawling it.

.DESCRIPTION
    A fresh Fly machine starts with an empty database and would need
    roughly half a day of crawling to reach what this PC already has.
    This copies it across in a couple of minutes.

    Two files go over, and the size difference between them is the whole
    reason this script exists rather than a plain `sftp put`:

      analytics.db    the derived store the API serves. Indexes are 335 MB
                      of its 567 MB and rebuild remotely in seconds, so
                      they are dropped before transfer and recreated on
                      arrival: 567 MB -> 101 MB compressed.

      valheatmap.db   the crawler's record of which matches it already
                      has (~0.4 MB). Without it the remote crawler would
                      re-fetch all 33,000 matches it is being handed.

    data/raw/ is deliberately NOT copied: 12.85 GB against a 20 GB volume,
    and nothing at runtime reads it. Only a full `build_analytics
    --rebuild` needs the payloads, and that can run here instead.

    The crawler is stopped for the swap. It writes to the same file, so
    replacing it underneath a running crawler risks a corrupt database.

.PARAMETER App
    Fly app name. Defaults to valheatmap.

.PARAMETER SkipCrawlerDb
    Send only analytics.db. The remote crawler will then re-fetch matches
    it already has, so use this only if the crawler index is suspect.

.EXAMPLE
    .\deploy\seed-fly.ps1
#>
[CmdletBinding()]
param(
    [string]$App = "valheatmap",
    [switch]$SkipCrawlerDb
)

$ErrorActionPreference = "Stop"

# flyctl writes progress ("Connecting to ...") to stderr even when it
# succeeds. Under ErrorActionPreference=Stop PowerShell turns that into a
# terminating error, so every flyctl call goes through here with stderr
# merged into stdout.
function Invoke-Fly {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$FlyArgs)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & fly @FlyArgs 2>&1 | ForEach-Object { "$_" }
        return @{ Ok = ($LASTEXITCODE -eq 0); Output = ($output -join "`n") }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# `fly ssh console` on Windows exits 1 even on success -- it cannot attach
# a console and reports "The handle is invalid" after the command has
# already run correctly. Its exit code therefore says nothing, so remote
# commands end with a sentinel and we look for it in the output.
$SENTINEL = "__VHM_REMOTE_OK__"

function Invoke-Remote {
    param(
        [Parameter(Mandatory)][string]$App,
        [Parameter(Mandatory)][string]$Script,
        [string]$Name = "remote command"
    )
    # Sent as a file rather than inline: quoting survives neither
    # PowerShell nor flyctl reliably once the script contains Python.
    $token = [guid]::NewGuid().ToString('N')
    $local = Join-Path $env:TEMP "vhm-remote-$token.sh"
    # `fly sftp put` refuses to overwrite, so every call needs a fresh
    # remote name; the script removes itself once it has run.
    $remotePath = "/data/.vhm-$token.sh"
    $body = "set -e`n$Script`necho $SENTINEL`nrm -f $remotePath`n"
    [IO.File]::WriteAllText($local, ($body -replace "`r`n", "`n"))
    try {
        $put = Invoke-Fly ssh sftp put --app $App $local $remotePath
        if (-not $put.Ok) { throw "Could not send $Name`:`n$($put.Output)" }
        $run = Invoke-Fly ssh console --app $App -C "sh $remotePath"
        $ok = $run.Output -match [regex]::Escape($SENTINEL)
        # Strip the sentinel and flyctl's own noise from what we show.
        $clean = ($run.Output -split "`n" | Where-Object {
            $_ -notmatch [regex]::Escape($SENTINEL) -and
            $_ -notmatch "^Connecting to " -and
            $_ -notmatch "^Error: The handle is invalid"
        }) -join "`n"
        return @{ Ok = [bool]$ok; Output = $clean }
    } finally {
        Remove-Item $local -ErrorAction SilentlyContinue
    }
}

$repo = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $repo "data"
$analytics = Join-Path $dataDir "analytics.db"
$crawlerDb = Join-Path $dataDir "valheatmap.db"

$work = Join-Path $env:TEMP "valheatmap-seed"
New-Item -ItemType Directory -Force -Path $work | Out-Null
$stripped = Join-Path $work "analytics-noidx.db"
$packed = Join-Path $work "analytics-noidx.db.zst"

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }
function Note($text) { Write-Host "    $text" -ForegroundColor DarkGray }

if (-not (Test-Path $analytics)) {
    throw "No database at $analytics. Run: cd backend; python -m app.build_analytics"
}

# zstd roughly halves gzip's output here (101 MB vs 268 MB) and the
# transfer runs at ~0.75 MB/s, so the choice is worth about 3.5 minutes.
# It needs the tool here AND the zstandard module in the image; checking
# both now avoids discovering the gap after a multi-minute upload.
$useZstd = [bool](Get-Command zstd -ErrorAction SilentlyContinue)
if (-not $useZstd) {
    Note "zstd not found locally; using gzip (transfers ~2.5x more data)"
} else {
    $probe = Invoke-Remote -App $App -Name "zstandard probe" `
        -Script "python -c 'import zstandard'"
    if (-not $probe.Ok) {
        $useZstd = $false
        Note "remote image lacks the zstandard module; using gzip instead"
        Note "a 'fly deploy' picks it up from requirements.txt and speeds this up"
    }
}

# --- 1. strip the rebuildable indexes -----------------------------------
Step "Preparing a copy without indexes"
foreach ($suffix in @("", "-wal", "-shm")) {
    Remove-Item "$stripped$suffix" -ErrorAction SilentlyContinue
}
Copy-Item $analytics $stripped

$dropScript = @'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
names = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")]
for name in names:
    conn.execute(f"DROP INDEX IF EXISTS {name}")
conn.commit()
conn.execute("VACUUM")
conn.commit()
# A corrupt upload is worse than a slow one: check before it goes over.
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity check failed"
counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for t in ("matches", "kills", "plants")}
conn.close()
print(f"dropped {len(names)} indexes")
for table, n in counts.items():
    print(f"  {table:9s} {n:>12,}")
'@
$dropFile = Join-Path $work "strip.py"
Set-Content -Path $dropFile -Value $dropScript -Encoding UTF8
python $dropFile $stripped
if ($LASTEXITCODE -ne 0) { throw "Failed to strip indexes" }

$beforeMb = [math]::Round((Get-Item $analytics).Length / 1MB, 1)
$afterMb = [math]::Round((Get-Item $stripped).Length / 1MB, 1)
Note "$beforeMb MB -> $afterMb MB before compression"

# --- 2. compress ---------------------------------------------------------
Step "Compressing"
Remove-Item $packed -ErrorAction SilentlyContinue
if ($useZstd) {
    $ErrorActionPreference = "Continue"
    zstd -12 -T0 -f -q -o $packed $stripped 2>&1 | Out-Null
    $zstdOk = $LASTEXITCODE -eq 0
    $ErrorActionPreference = "Stop"
    if (-not $zstdOk) { throw "zstd failed" }
} else {
    $packed = Join-Path $work "analytics-noidx.db.gz"
    Remove-Item $packed -ErrorAction SilentlyContinue
    $in = [IO.File]::OpenRead($stripped)
    $out = [IO.File]::Create($packed)
    $gz = New-Object IO.Compression.GZipStream($out, [IO.Compression.CompressionLevel]::Optimal)
    $in.CopyTo($gz, 1MB)
    $gz.Dispose(); $out.Dispose(); $in.Dispose()
}
$packedMb = [math]::Round((Get-Item $packed).Length / 1MB, 1)
Note "$packedMb MB to transfer (~$([math]::Round($packedMb / 0.75 / 60, 1)) min at observed speed)"

# --- 3. stop the crawler -------------------------------------------------
# Set before uploading, not after: the upload takes minutes, and the
# crawler must not be writing to analytics.db when the swap happens.
Step "Stopping the remote crawler for the swap"
$stop = Invoke-Fly secrets set VALHEATMAP_CRAWLER=0 --app $App
if (-not $stop.Ok) { throw "Could not set VALHEATMAP_CRAWLER:`n$($stop.Output)" }
Note "machine is restarting with the crawler disabled"

# The secret change restarts the machine; wait for it to answer again
# before pushing a file at it.
$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 5
    $up = (Invoke-Remote -App $App -Name "readiness probe" -Script "true").Ok
} while (-not $up -and (Get-Date) -lt $deadline)
if (-not $up) { throw "Machine did not come back after restart" }
Note "machine is back"

# --- 4. upload -----------------------------------------------------------
Step "Uploading the database"
$remotePacked = "/data/incoming" + [IO.Path]::GetExtension($packed)
# sftp will not overwrite, so clear anything a previous run left behind.
Invoke-Remote -App $App -Name "pre-upload cleanup" -Script @"
rm -f /data/incoming.zst /data/incoming.gz /data/analytics.db.new
rm -f /data/valheatmap-incoming.db /data/install.py
"@ | Out-Null

$sw = [Diagnostics.Stopwatch]::StartNew()
$put = Invoke-Fly ssh sftp put --app $App $packed $remotePacked
if (-not $put.Ok) { throw "Upload failed:`n$($put.Output)" }
$sw.Stop()
Note "sent in $([math]::Round($sw.Elapsed.TotalMinutes, 1)) min"

if (-not $SkipCrawlerDb -and (Test-Path $crawlerDb)) {
    Step "Uploading the crawler index"
    # Copy first: this file is in WAL mode and may be mid-write locally.
    $crawlerCopy = Join-Path $work "valheatmap.db"
    $copyScript = @'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)   # consistent copy even while the local crawler writes
dst.close(); src.close()
'@
    $copyFile = Join-Path $work "copydb.py"
    Set-Content -Path $copyFile -Value $copyScript -Encoding UTF8
    Remove-Item $crawlerCopy -ErrorAction SilentlyContinue
    python $copyFile $crawlerDb.Replace('\', '/') $crawlerCopy.Replace('\', '/')
    if ($LASTEXITCODE -ne 0) { throw "Could not copy the crawler index" }
    $putIdx = Invoke-Fly ssh sftp put --app $App $crawlerCopy "/data/valheatmap-incoming.db"
    if (-not $putIdx.Ok) { throw "Crawler index upload failed:`n$($putIdx.Output)" }
}

# --- 5. swap and rebuild indexes ----------------------------------------
Step "Installing it on the volume and rebuilding indexes"

# Sent as a Python file rather than `python -c` inside sh inside
# PowerShell: three layers of quoting is a reliable way to break things.
$installPy = @'
"""Unpack the uploaded database, swap it in, rebuild the indexes."""
import gzip
import os
import shutil
import sqlite3
import sys
from pathlib import Path

DATA = Path("/data")
target = DATA / "analytics.db"
staged = DATA / "analytics.db.new"

packed = next((p for p in (DATA / "incoming.zst", DATA / "incoming.gz") if p.exists()), None)
if packed is None:
    sys.exit("nothing uploaded: no /data/incoming.*")

print(f"unpacking {packed.name} ({packed.stat().st_size / 1e6:.0f} MB)")
if packed.suffix == ".zst":
    import zstandard

    with packed.open("rb") as src, staged.open("wb") as dst:
        zstandard.ZstdDecompressor().copy_stream(src, dst)
else:
    with gzip.open(packed, "rb") as src, staged.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1 << 20)

# Verify before destroying the file being replaced: a half-written upload
# must not become the live database.
check = sqlite3.connect(staged)
state = check.execute("PRAGMA integrity_check").fetchone()[0]
counts = {
    t: check.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    for t in ("matches", "kills", "plants")
}
check.close()
if state != "ok" or counts["matches"] == 0:
    sys.exit(f"uploaded database is unusable (integrity={state}, {counts})")
print(f"unpacked ok: {counts['matches']:,} matches, {counts['kills']:,} kills")

# A stale WAL belongs to the old file and would corrupt the new one.
for suffix in ("-wal", "-shm"):
    Path(str(target) + suffix).unlink(missing_ok=True)
os.replace(staged, target)
packed.unlink(missing_ok=True)

incoming_idx = DATA / "valheatmap-incoming.db"
if incoming_idx.exists():
    crawler_db = DATA / "valheatmap.db"
    for suffix in ("-wal", "-shm"):
        Path(str(crawler_db) + suffix).unlink(missing_ok=True)
    os.replace(incoming_idx, crawler_db)
    known = sqlite3.connect(crawler_db).execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"crawler index installed: {known:,} matches already known")

sys.path.insert(0, "/app/backend")
from app.slim import ensure_indexes  # noqa: E402

print("rebuilding indexes...")
print(f"indexes rebuilt in {ensure_indexes(target):.0f}s")

# Without this /api/facets recomputes over every kill on each request --
# 40s+ at this size. build_slim does it for published snapshots; a seeded
# database has to do it here.
from app.analytics_db import AnalyticsDB  # noqa: E402

print("building the facet cache...")
AnalyticsDB(target).rebuild_facet_cache()
print("facet cache built")

usage = shutil.disk_usage(DATA)
print(f"volume: {usage.used / 1e9:.1f} GB used, {usage.free / 1e9:.1f} GB free")
print(f"database: {target.stat().st_size / 1e6:.0f} MB")
'@

$installFile = Join-Path $work "install.py"
[IO.File]::WriteAllText($installFile, ($installPy -replace "`r`n", "`n"))
$putPy = Invoke-Fly ssh sftp put --app $App $installFile "/data/install.py"
if (-not $putPy.Ok) { throw "Could not upload the install script:`n$($putPy.Output)" }

$install = Invoke-Remote -App $App -Name "install" -Script "python /data/install.py"
Write-Host $install.Output
$installOk = $install.Ok

# --- 6. restart the crawler ---------------------------------------------
# Runs even if the swap failed: leaving the crawler off is worse than a
# failed seed, because then nothing is collecting data at all.
Step "Re-enabling the crawler"
$resume = Invoke-Fly secrets unset VALHEATMAP_CRAWLER --app $App
if (-not $resume.Ok) {
    Write-Host "Could not re-enable the crawler. Run this yourself:" -ForegroundColor Yellow
    Write-Host "  fly secrets unset VALHEATMAP_CRAWLER --app $App" -ForegroundColor Yellow
} else {
    Note "crawler is back on; the machine is restarting"
}

Invoke-Remote -App $App -Name "cleanup" `
    -Script "rm -f /data/install.py /data/.vhm-remote.sh" | Out-Null

if (-not $installOk) { throw "The install step failed; see the output above." }

Write-Host "`nDone. Check it:" -ForegroundColor Green
Write-Host "  curl https://$App.fly.dev/api/health"
Write-Host "  fly logs --app $App"
