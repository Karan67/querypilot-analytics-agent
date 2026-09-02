<#
    Fetch the Chinook PostgreSQL dump into db/seed/chinook.sql.

    Windows equivalent of fetch_chinook.sh. Run on the HOST, once, before the
    first `docker compose up`.

        .\db\fetch_chinook.ps1
        .\db\fetch_chinook.ps1 -Force

    Override the source with -Url if the upstream path ever moves.
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [string]$Url = "https://raw.githubusercontent.com/lerocha/chinook-database/master/ChinookDatabase/DataSources/Chinook_PostgreSql.sql"
)

$ErrorActionPreference = "Stop"

$seedDir  = Join-Path $PSScriptRoot "seed"
$seedFile = Join-Path $seedDir "chinook.sql"

if ((Test-Path $seedFile) -and (-not $Force)) {
    Write-Host "Seed already present: $seedFile"
    Write-Host "Re-download with: .\db\fetch_chinook.ps1 -Force"
    exit 0
}

if (-not (Test-Path $seedDir)) {
    New-Item -ItemType Directory -Path $seedDir | Out-Null
}

Write-Host "Downloading Chinook from:"
Write-Host "  $Url"

$raw = (Invoke-WebRequest -Uri $Url -UseBasicParsing).Content

# The upstream dump manages its own database: it opens with
#   DROP DATABASE IF EXISTS chinook; CREATE DATABASE chinook; \c chinook
# Inside the Postgres init hook we are already connected to POSTGRES_DB, and a
# session cannot drop the database it is connected to. Strip that preamble so
# the file loads into whichever database psql is pointed at.
$lines = $raw -split "`r?`n"
$kept = $lines | Where-Object {
    ($_ -notmatch '^\s*(?i:(drop|create)\s+database\s)') -and
    ($_ -notmatch '^\s*\\(?i:c|connect)(\s|;|$)')
}

# LF endings: this file is read by psql inside a Linux container.
$content = ($kept -join "`n")
[System.IO.File]::WriteAllText($seedFile, $content, (New-Object System.Text.UTF8Encoding($false)))

$tables = ([regex]::Matches($content, '(?im)^\s*create\s+table\s')).Count
# [^\S\r\n] is "whitespace but not a line break". Plain \s would match the
# newline after the comment line "   Create database" in the upstream header
# and report a leftover that is not there.
$leftover = ([regex]::Matches($content, '(?im)^[^\S\r\n]*(drop|create)[^\S\r\n]+database[^\S\r\n]')).Count
$bytes = (Get-Item $seedFile).Length

Write-Host ""
Write-Host "Wrote $seedFile ($bytes bytes)"
Write-Host "  CREATE TABLE statements : $tables"
Write-Host "  leftover CREATE/DROP DATABASE : $leftover (expected 0)"

if ($tables -eq 0) {
    Write-Error "No CREATE TABLE found. The download is probably an HTML error page rather than SQL. Check the -Url value."
    exit 1
}

Write-Host ""
Write-Host "Next: docker compose up --build"
Write-Host "(If the db volume already exists, run 'docker compose down -v' first --"
Write-Host " Postgres init scripts only run on an empty data volume.)"
