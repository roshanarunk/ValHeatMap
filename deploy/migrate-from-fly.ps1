<#
.SYNOPSIS
    Move production data (analytics.db, valheatmap.db, raw/) from Fly.io to Hetzner.

.DESCRIPTION
    1. Checks the Fly volume has room for the export, and stops if not.
    2. Pauses the Fly crawler for good (VALHEATMAP_CRAWLER=0). From here on,
       Hetzner is the copy that collects data.
    3. On Fly, takes consistent snapshots of both databases with SQLite's
       backup API (safe while the API keeps serving), gzips them, and tars
       raw/ uncompressed (it is mostly .zip already). Runs detached on the
       machine, so a dropped SSH session cannot kill it halfway.
    4. Downloads the files and checks their SHA-256 against the ones
       computed on Fly.
    5. With -HetznerHost: uploads, verifies again, unpacks, and runs
       SQLite's quick_check on the result.

    The Fly site keeps serving the whole time. Expect an hour or more at
    ~14 GB of database and ~7 GB of raw payloads; most of it is transfer.

.PARAMETER HetznerHost
    SSH login for the Hetzner server, e.g. root@203.0.113.10. Without it,
    the files are only downloaded to -LocalDir.

.PARAMETER SkipRaw
    Leave raw/ behind. The site does not need it; only the backfill
    scripts re-read it.

.EXAMPLE
    .\deploy\migrate-from-fly.ps1 -HetznerHost root@203.0.113.10
#>

[CmdletBinding()]
param(
    [string]$App = "valheatmap",
    [string]$HetznerHost = "",
    [string]$HetznerDataDir = "/opt/valheatmap/data",
    [string]$LocalDir = "",
    [switch]$SkipRaw
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $LocalDir) { $LocalDir = Join-Path $repo "data\migration" }

function Step($text) { Write-Host "`n=== $text" -ForegroundColor Cyan }

# `fly ssh console` on Windows exits non-zero even on success ("The handle
# is invalid"), so callers check the output for markers instead.
function Invoke-FlySh([string]$Command) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = fly ssh console --app $App -C "sh -c '$Command'" 2>&1
        return (($out | ForEach-Object { "$_" }) |
            Where-Object { $_ -notmatch "^Connecting to|handle is invalid" }) -join "`n"
    } finally {
        $ErrorActionPreference = $prev
    }
}

# Python sent base64-encoded, so no quoting survives three shells.
function Invoke-FlyPython([string]$Code) {
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code.Replace("`r", "")))
    return Invoke-FlySh "echo $b64 | base64 -d | python"
}

function Invoke-Fly([string[]]$FlyArgs) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & fly @FlyArgs 2>&1 | ForEach-Object { "$_" }
        return [pscustomobject]@{ Code = $LASTEXITCODE; Output = ($out -join "`n") }
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Assert-Native([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)." }
}

# Runs on the Fly machine. Kept as Python because the image has no sqlite3
# CLI, and the backup API is what makes a snapshot of a live database safe.
$exportScript = @'
import hashlib, json, os, shutil, sqlite3, subprocess, sys, time, traceback

OUT, LOG = "/data/migrate", "/data/migrate.log"
SKIP_RAW = "skip-raw" in sys.argv[1:]

def log(msg):
    free = shutil.disk_usage("/data").free / 1e9
    with open(LOG, "a") as fh:
        fh.write(f"{time.strftime('%H:%M:%S')} {msg} (free {free:.1f} GB)\n")

def run(cmd):
    subprocess.run(cmd, shell=True, check=True)

try:
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    manifest = {}
    for name in ("analytics.db", "valheatmap.db"):
        src = f"/data/{name}"
        if not os.path.exists(src):
            continue
        log(f"snapshotting {name}")
        s = sqlite3.connect(src, timeout=120)
        d = sqlite3.connect(f"{OUT}/{name}")
        s.backup(d)  # one step: a single read transaction, so consistent
        s.close()
        # A self-contained file, so no -wal has to travel with it.
        d.execute("PRAGMA journal_mode=DELETE")
        if name == "analytics.db":
            manifest["kills_max_rowid"] = d.execute("SELECT MAX(rowid) FROM kills").fetchone()[0]
            manifest["matches"] = d.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        d.close()
        log(f"compressing {name}")
        run(f"gzip -6 {OUT}/{name}")
    if not SKIP_RAW and os.path.isdir("/data/raw"):
        log("archiving raw/")
        run(f"tar -cf {OUT}/raw.tar -C /data raw")
    with open(f"{OUT}/manifest.json", "w") as fh:
        json.dump(manifest, fh)
    log("checksumming")
    with open(f"{OUT}/SHA256SUMS", "w", newline="\n") as sums:
        for name in sorted(os.listdir(OUT)):
            if name == "SHA256SUMS":
                continue
            digest = hashlib.sha256()
            with open(f"{OUT}/{name}", "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            sums.write(f"{digest.hexdigest()}  {name}\n")
    log(f"manifest {json.dumps(manifest)}")
    log("EXPORT-DONE")
except Exception:
    log("EXPORT-FAILED " + traceback.format_exc().replace("\n", " | "))
    # A full volume stops the live site's writes too; never leave it full.
    shutil.rmtree(OUT, ignore_errors=True)
'@

Write-Host "ValHeatMap migration: Fly.io -> Hetzner" -ForegroundColor Green

if (-not (Get-Command fly -ErrorAction SilentlyContinue)) {
    throw "fly CLI not found in PATH."
}
if ($HetznerHost -and -not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "ssh not found in PATH (Windows: Settings > Optional features > OpenSSH Client)."
}

# --- 1. room on the Fly volume -----------------------------------------
Step "Checking space on the Fly volume"
$sizes = Invoke-FlyPython @'
import os, shutil
size = lambda p: os.path.getsize(p) if os.path.exists(p) else 0
raw = sum(e.stat().st_size for e in os.scandir("/data/raw")) if os.path.isdir("/data/raw") else 0
dbs = size("/data/analytics.db") + size("/data/valheatmap.db")
print("SIZES", dbs, raw, shutil.disk_usage("/data").free)
'@
$line = ($sizes -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -match "^SIZES " } | Select-Object -First 1)
if (-not $line) { throw "Could not read sizes from Fly:`n$sizes" }
$null, $dbBytes, $rawBytes, $freeBytes = $line -split " " | ForEach-Object { [double]$_ }
if ($SkipRaw) { $rawBytes = 0 }
# Peak use is while the biggest snapshot is being gzipped: the snapshot
# plus its growing .gz. Afterwards it is the .gz files plus raw.tar.
$need = [math]::Max(1.5 * $dbBytes, 0.5 * $dbBytes + $rawBytes) + 1GB
"{0:N1} GB of databases, {1:N1} GB of raw payloads, {2:N1} GB free, ~{3:N1} GB needed" -f `
    ($dbBytes / 1GB), ($rawBytes / 1GB), ($freeBytes / 1GB), ($need / 1GB) | Write-Host
if ($freeBytes -lt $need) {
    $vols = Invoke-Fly @("volumes", "list", "--app", $App, "--json")
    $vol = ($vols.Output | ConvertFrom-Json) | Select-Object -First 1
    $target = [math]::Ceiling(($vol.size_gb * 1GB - $freeBytes + $need) / 1GB / 10) * 10
    throw ("Not enough room on the Fly volume. Grow it first (billed per GB, " +
        "and the volume is deleted with the app afterwards):`n" +
        "    fly volumes extend $($vol.id) -s $target --app $App`n" +
        "then run this script again.")
}

# --- 2. stop Fly collecting --------------------------------------------
Step "Pausing the Fly crawler (restarts the machine; the site blips briefly)"
$res = Invoke-Fly @("secrets", "set", "VALHEATMAP_CRAWLER=0", "--app", $App)
Write-Host $res.Output -ForegroundColor Gray
if ($res.Code -ne 0) {
    Write-Host "fly secrets set exited $($res.Code); checking the machine directly." -ForegroundColor Yellow
}
# Ask the machine itself. An answer is required: an SSH attempt that fails
# mid-restart returns nothing, which must not read as "not running".
$crawlerCheck = @'
import os
running = False
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        running |= b"app.crawler" in open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        pass
print("CRAWLER_RUNNING" if running else "CRAWLER_STOPPED")
'@
$state = ""
for ($i = 0; $i -lt 8 -and $state -notmatch "CRAWLER_(RUNNING|STOPPED)"; $i++) {
    if ($i) { Start-Sleep -Seconds 15 }
    $state = Invoke-FlyPython $crawlerCheck
}
if ($state -notmatch "CRAWLER_STOPPED") {
    throw "The crawler is still running on Fly (or the machine did not answer). Check 'fly status' and rerun."
}
Write-Host "Crawler is not running on Fly." -ForegroundColor Gray

# --- 3. export on Fly ----------------------------------------------------
Step "Exporting on Fly (runs detached; polling every 30s)"
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($exportScript))
$rawArg = if ($SkipRaw) { "skip-raw" } else { "" }
$started = Invoke-FlySh "rm -f /data/migrate.log; echo $b64 | base64 -d > /data/export.py && cd /data && setsid nohup python /data/export.py $rawArg > /dev/null 2>&1 & sleep 2; echo STARTED"
if ($started -notmatch "STARTED") { throw "Could not start the export on Fly:`n$started" }

$deadline = (Get-Date).AddHours(4)
$lastLine = ""
while ($true) {
    Start-Sleep -Seconds 30
    $tail = Invoke-FlySh "tail -1 /data/migrate.log 2>/dev/null"
    if ($tail -and $tail -ne $lastLine) { Write-Host "  $tail" -ForegroundColor Gray; $lastLine = $tail }
    if ($tail -match "EXPORT-DONE") { break }
    if ($tail -match "EXPORT-FAILED") { throw "Export failed on Fly; nothing was left on the volume." }
    if ((Get-Date) -gt $deadline) { throw "Export still running after 4 hours; check /data/migrate.log on Fly." }
}

# --- 4. download and verify ---------------------------------------------
Step "Downloading to $LocalDir"
New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null
$sums = Invoke-FlySh "cat /data/migrate/SHA256SUMS"
$expected = @{}
foreach ($l in ($sums -split "`n" | ForEach-Object { $_.Trim() })) {
    if ($l -match "^([0-9a-f]{64})\s+\*?(\S+)$") { $expected[$Matches[2]] = $Matches[1] }
}
if ($expected.Count -eq 0) { throw "No checksums found on Fly:`n$sums" }

foreach ($name in $expected.Keys) {
    $local = Join-Path $LocalDir $name
    if (Test-Path $local) { Remove-Item $local -Force }
    Write-Host "  $name" -ForegroundColor Gray
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    fly ssh sftp get "/data/migrate/$name" $local --app $App 2>&1 | Out-Host
    $ErrorActionPreference = $prev
    if (-not (Test-Path $local)) { throw "Download of $name failed." }
    $hash = (Get-FileHash $local -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $expected[$name]) { throw "$name is corrupt after download (checksum mismatch). Rerun to try again." }
    "    {0:N2} GB, checksum OK" -f ((Get-Item $local).Length / 1GB) | Write-Host -ForegroundColor Gray
}
Set-Content -Path (Join-Path $LocalDir "SHA256SUMS") -Value (($expected.GetEnumerator() |
    ForEach-Object { "$($_.Value)  $($_.Key)" }) -join "`n") -NoNewline -Encoding ascii

Step "Removing the export from the Fly volume"
Invoke-FlySh "rm -rf /data/migrate /data/export.py /data/migrate.log /data/build_idx.py /data/idx.log; echo CLEANED" | Out-Null

# --- 5. upload to Hetzner ------------------------------------------------
if (-not $HetznerHost) {
    Write-Host "`nFiles are in $LocalDir. Rerun with -HetznerHost root@<IP> to upload them," -ForegroundColor Yellow
    Write-Host "or follow 'Option B' in deploy/HETZNER.md." -ForegroundColor Yellow
    return
}

Step "Uploading to $HetznerHost"
$running = ssh $HetznerHost "docker ps -q -f name=valheatmap-app 2>/dev/null"
if ($running) {
    throw "valheatmap-app is running on Hetzner. Stop it first (docker compose down), or it will hold the old database open."
}
ssh $HetznerHost "mkdir -p '$HetznerDataDir/incoming'"; Assert-Native "mkdir on Hetzner"
foreach ($name in @($expected.Keys) + "SHA256SUMS") {
    Write-Host "  $name" -ForegroundColor Gray
    scp (Join-Path $LocalDir $name) "${HetznerHost}:$HetznerDataDir/incoming/$name"; Assert-Native "Upload of $name"
}

Step "Verifying and unpacking on Hetzner"
# Stale -wal/-shm files from an earlier start would be replayed onto the
# new database and corrupt it, so they go before anything is unpacked.
$unpack = @"
set -e
cd '$HetznerDataDir/incoming'
sha256sum -c SHA256SUMS
cd '$HetznerDataDir'
rm -f analytics.db analytics.db-wal analytics.db-shm valheatmap.db valheatmap.db-wal valheatmap.db-shm
for f in incoming/*.db.gz; do gunzip -c "`$f" > "`$(basename "`$f" .gz)"; done
if [ -f incoming/raw.tar ]; then tar -xf incoming/raw.tar; fi
python3 - <<'PY'
import json, sqlite3
m = json.load(open("incoming/manifest.json"))
c = sqlite3.connect("analytics.db")
print("quick_check:", c.execute("PRAGMA quick_check").fetchone()[0])
got = c.execute("SELECT MAX(rowid) FROM kills").fetchone()[0]
matches = c.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
print(f"kills max rowid {got} (Fly: {m['kills_max_rowid']}), matches {matches} (Fly: {m['matches']})")
assert got == m["kills_max_rowid"] and matches == m["matches"], "row counts differ"
PY
rm -rf incoming
ls -lah '$HetznerDataDir'
echo UNPACKED
"@
$out = $unpack.Replace("`r", "") | ssh $HetznerHost "bash -s" 2>&1
$out | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
if (($out -join "`n") -notmatch "UNPACKED") { throw "Unpacking on Hetzner failed; see the output above." }

Write-Host "`nData is on Hetzner and verified. Next: section 4 of deploy/HETZNER.md (docker compose up)." -ForegroundColor Green
Write-Host "Local copies are still in $LocalDir; delete them once the site is live on Hetzner." -ForegroundColor Green
