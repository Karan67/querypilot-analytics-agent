<#
    Fetch the Pagila PostgreSQL dump into db/pagila-seed/.

    Windows equivalent of fetch_pagila.sh. Iteration 14
    (specs/017-schema-generality.md): Pagila is the second, non-Chinook schema
    used to prove the engine reads a live catalog rather than Chinook's own.
    Run on the HOST, once, before `docker compose --profile pagila up -d
    pagila-db`.

        .\db\fetch_pagila.ps1
        .\db\fetch_pagila.ps1 -Force

    Override the source with -SchemaUrl / -DataUrl if the upstream path ever
    moves. Unlike Chinook's dump, neither Pagila file needs DROP/CREATE
    DATABASE stripping -- confirmed by reading both (2026-09-16): no
    database-management statements, no `\c`.
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [string]$SchemaUrl = "https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-schema.sql",
    [string]$DataUrl = "https://raw.githubusercontent.com/devrimgunduz/pagila/master/pagila-data.sql"
)

$ErrorActionPreference = "Stop"

$seedDir    = Join-Path $PSScriptRoot "pagila-seed"
$schemaFile = Join-Path $seedDir "pagila-schema.sql"
$dataFile   = Join-Path $seedDir "pagila-data.sql"

if ((Test-Path $schemaFile) -and (Test-Path $dataFile) -and (-not $Force)) {
    Write-Host "Seed already present: $schemaFile, $dataFile"
    Write-Host "Re-download with: .\db\fetch_pagila.ps1 -Force"
    exit 0
}

if (-not (Test-Path $seedDir)) {
    New-Item -ItemType Directory -Path $seedDir | Out-Null
}

function Get-Utf8NoBom {
    param([string]$Url, [string]$OutFile)
    Write-Host "Downloading: $Url"
    $content = (Invoke-WebRequest -Uri $Url -UseBasicParsing).Content
    [System.IO.File]::WriteAllText($OutFile, $content, (New-Object System.Text.UTF8Encoding($false)))
}

Get-Utf8NoBom -Url $SchemaUrl -OutFile $schemaFile
Get-Utf8NoBom -Url $DataUrl -OutFile $dataFile

$schemaContent = Get-Content $schemaFile -Raw
$tables = ([regex]::Matches($schemaContent, '(?im)^\s*create\s+table\s')).Count
$schemaBytes = (Get-Item $schemaFile).Length
$dataBytes = (Get-Item $dataFile).Length

Write-Host ""
Write-Host "Wrote $schemaFile ($schemaBytes bytes), $dataFile ($dataBytes bytes)"
Write-Host "  CREATE TABLE statements : $tables"

if ($tables -eq 0) {
    Write-Error "No CREATE TABLE found in the schema file. The download is probably an HTML error page rather than SQL. Check -SchemaUrl."
    exit 1
}

Write-Host ""
Write-Host "Next: docker compose --profile pagila up -d pagila-db"
Write-Host "(If the pagila_data volume already exists, run"
Write-Host " 'docker compose --profile pagila down -v' first -- Postgres init"
Write-Host " scripts only run on an empty data volume.)"
