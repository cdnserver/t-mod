param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [string]$PersistentDir = "C:\Users\Admin\Documents\SGLDiscordBot",
    [string]$Branch = "main",
    [string]$Remote = "origin"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$UpdateRoot = Join-Path $PersistentDir "updates"
$StatusPath = Join-Path $UpdateRoot "status.json"
$CandidateDir = $null
$CandidateDbDir = $null
$OldCommit = "unknown"
$TargetCommit = "unknown"
$BackupPath = $null
$RollbackImage = $null
$RollbackSupervisorImage = $null
$UpdateMutex = New-Object System.Threading.Mutex($false, "Local\TModSafeUpdate")
$MutexAcquired = $UpdateMutex.WaitOne(0)
if (-not $MutexAcquired) {
    Write-Host "[SAFE UPDATE] Another T-Mod update or startup is already running." -ForegroundColor Yellow
    exit 0
}

function Write-UpdateStatus {
    param(
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Message,
        [hashtable]$Extra = @{}
    )
    New-Item -ItemType Directory -Path $UpdateRoot -Force | Out-Null
    $payload = @{
        state = $State
        message = $Message
        old_commit = $OldCommit
        new_commit = $TargetCommit
        release = if ($State -eq "rolled_back") { $OldCommit } else { $TargetCommit }
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    foreach ($key in $Extra.Keys) { $payload[$key] = $Extra[$key] }
    $temporary = "$StatusPath.tmp"
    $json = $payload | ConvertTo-Json -Depth 8
    $utf8NoBom = New-Object System.Text.UTF8Encoding
    [IO.File]::WriteAllText($temporary, $json, $utf8NoBom)
    Move-Item -LiteralPath $temporary -Destination $StatusPath -Force
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed with exit code $LASTEXITCODE"
    }
}

function Test-ImageExists([string]$Image) {
    & docker image inspect $Image *> $null
    return $LASTEXITCODE -eq 0
}

function Invoke-CurrentRuntime {
    param([string]$Directory, [bool]$SkipBuild)
    $previousSkip = $env:TMOD_SKIP_BUILD
    $previousNonInteractive = $env:TMOD_NONINTERACTIVE
    $previousTransactional = $env:TMOD_TRANSACTIONAL_UPDATE
    try {
        $env:TMOD_SKIP_BUILD = if ($SkipBuild) { "1" } else { "0" }
        $env:TMOD_NONINTERACTIVE = "1"
        $env:TMOD_TRANSACTIONAL_UPDATE = "1"
        Push-Location $Directory
        try {
            & cmd.exe /d /c "call run_windows.bat"
            return $LASTEXITCODE
        }
        finally { Pop-Location }
    }
    finally {
        $env:TMOD_SKIP_BUILD = $previousSkip
        $env:TMOD_NONINTERACTIVE = $previousNonInteractive
        $env:TMOD_TRANSACTIONAL_UPDATE = $previousTransactional
    }
}

function New-PreUpdateBackup {
    param([string]$Note)
    $output = $null
    & docker inspect tmod-discord-bot *> $null
    if ($LASTEXITCODE -eq 0) {
        $output = & docker exec tmod-discord-bot python /app/scripts/tmod_db_guard.py backup --kind pre-update --note $Note 2>&1
        if ($LASTEXITCODE -eq 0) {
            try { return (($output | Out-String | ConvertFrom-Json).result.path) } catch {}
        }
    }
    if (Test-ImageExists "tmod-discord-bot:latest") {
        $output = & docker run --rm --user 0:0 `
            --env "DATA_DIR=/app/persistent/data" `
            --env "DATABASE_FILE=/app/persistent/data/tmod.db" `
            --env "TMOD_DB_BACKUP_DIR=/app/persistent/backups/database" `
            --mount "type=bind,source=$PersistentDir,target=/app/persistent" `
            tmod-discord-bot:latest `
            python /app/scripts/tmod_db_guard.py backup --kind pre-update --note $Note 2>&1
        if ($LASTEXITCODE -eq 0) {
            try { return (($output | Out-String | ConvertFrom-Json).result.path) } catch {}
        }
    }
    $database = Join-Path $PersistentDir "data\tmod.db"
    if (-not (Test-Path -LiteralPath $database)) { return $null }
    $previousData = $env:DATA_DIR
    $previousDatabase = $env:DATABASE_FILE
    $previousBackups = $env:TMOD_DB_BACKUP_DIR
    try {
        $env:DATA_DIR = Join-Path $PersistentDir "data"
        $env:DATABASE_FILE = $database
        $env:TMOD_DB_BACKUP_DIR = Join-Path $PersistentDir "backups\database"
        $output = & py -3 (Join-Path $ProjectDir "scripts\tmod_db_guard.py") backup --kind pre-update --note $Note 2>&1
        if ($LASTEXITCODE -eq 0) {
            try { return (($output | Out-String | ConvertFrom-Json).result.path) } catch {}
        }
    }
    finally {
        $env:DATA_DIR = $previousData
        $env:DATABASE_FILE = $previousDatabase
        $env:TMOD_DB_BACKUP_DIR = $previousBackups
    }
    throw "Could not create a consistent pre-update database backup"
}

function Restore-CodeRevision([string]$Commit) {
    Invoke-Native git -C $ProjectDir switch --detach $Commit
    Invoke-Native git -C $ProjectDir branch --force $Branch $Commit
    Invoke-Native git -C $ProjectDir switch $Branch
}

function Resolve-HostBackupPath([string]$Path) {
    if ($Path.StartsWith("/app/persistent/")) {
        $relative = $Path.Substring("/app/persistent/".Length).Replace("/", "\")
        return Join-Path $PersistentDir $relative
    }
    return $Path
}

function Restore-DatabaseIfCorrupt {
    if (-not $BackupPath -or -not (Test-ImageExists "tmod-discord-bot:latest")) { return $false }
    $containerBackupPath = $BackupPath
    if ($BackupPath.StartsWith($PersistentDir, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relativeBackup = $BackupPath.Substring($PersistentDir.Length).TrimStart("\", "/").Replace("\", "/")
        $containerBackupPath = "/app/persistent/$relativeBackup"
    }
    Push-Location $ProjectDir
    try { & docker compose stop tmod-discord-bot *> $null }
    finally { Pop-Location }
    & docker run --rm --user 0:0 `
        --env "DATA_DIR=/app/persistent/data" `
        --env "DATABASE_FILE=/app/persistent/data/tmod.db" `
        --env "TMOD_DB_BACKUP_DIR=/app/persistent/backups/database" `
        --mount "type=bind,source=$PersistentDir,target=/app/persistent" `
        tmod-discord-bot:latest `
        python /app/scripts/tmod_db_guard.py check --full *> $null
    if ($LASTEXITCODE -eq 0) { return $false }
    & docker run --rm --user 0:0 `
        --env "DATA_DIR=/app/persistent/data" `
        --env "DATABASE_FILE=/app/persistent/data/tmod.db" `
        --env "TMOD_DB_BACKUP_DIR=/app/persistent/backups/database" `
        --mount "type=bind,source=$PersistentDir,target=/app/persistent" `
        tmod-discord-bot:latest `
        python /app/scripts/tmod_db_guard.py restore $containerBackupPath --offline-confirmed
    if ($LASTEXITCODE -ne 0) { throw "Database restore failed" }
    return $true
}

try {
    $ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
    New-Item -ItemType Directory -Path $UpdateRoot -Force | Out-Null
    $OldCommit = (& git -C $ProjectDir rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Project is not a Git repository" }

    Write-Host "[SAFE UPDATE] Fetching $Remote/$Branch (non-interactive, 20s stall limit) ..."
    $previousPrompt = $env:GIT_TERMINAL_PROMPT
    $env:GIT_TERMINAL_PROMPT = "0"
    try {
        & git -C $ProjectDir `
            -c credential.interactive=never `
            -c http.lowSpeedLimit=1 `
            -c http.lowSpeedTime=20 `
            fetch --prune $Remote "+refs/heads/${Branch}:refs/remotes/${Remote}/${Branch}"
        $fetchExitCode = $LASTEXITCODE
    }
    finally { $env:GIT_TERMINAL_PROMPT = $previousPrompt }
    if ($fetchExitCode -ne 0) {
        Write-UpdateStatus -State "offline" -Message "GitHub недоступен; запущена установленная версия."
        $runtimeCode = Invoke-CurrentRuntime -Directory $ProjectDir -SkipBuild $false
        exit $runtimeCode
    }
    $TargetCommit = (& git -C $ProjectDir rev-parse "$Remote/$Branch").Trim()

    $previousStatus = $null
    if (Test-Path -LiteralPath $StatusPath) {
        try { $previousStatus = Get-Content -Raw -LiteralPath $StatusPath | ConvertFrom-Json } catch {}
    }
    if (
        $null -ne $previousStatus -and
        $previousStatus.state -eq "rolled_back" -and
        $previousStatus.new_commit -eq $TargetCommit -and
        $OldCommit -ne $TargetCommit
    ) {
        Write-Host "[SAFE UPDATE] This release was rolled back earlier; waiting for a newer commit."
        $runtimeCode = Invoke-CurrentRuntime -Directory $ProjectDir -SkipBuild $false
        exit $runtimeCode
    }

    if ($OldCommit -eq $TargetCommit) {
        Write-UpdateStatus -State "success" -Message "Установленная версия актуальна."
        $runtimeCode = Invoke-CurrentRuntime -Directory $ProjectDir -SkipBuild $false
        exit $runtimeCode
    }

    $dirty = (& git -C $ProjectDir status --porcelain --untracked-files=normal | Out-String).Trim()
    if ($dirty) {
        Write-UpdateStatus -State "blocked_local_changes" -Message "Автообновление отложено: в серверной копии есть локальные изменения. Файлы не изменялись и не прятались в stash."
        Write-Host "[SAFE UPDATE] Local changes detected; preserving them and starting the installed release." -ForegroundColor Yellow
        $runtimeCode = Invoke-CurrentRuntime -Directory $ProjectDir -SkipBuild $false
        exit $runtimeCode
    }

    $shortOld = $OldCommit.Substring(0, [Math]::Min(12, $OldCommit.Length))
    $shortTarget = $TargetCommit.Substring(0, [Math]::Min(12, $TargetCommit.Length))
    Write-UpdateStatus -State "testing" -Message "Кандидат $shortTarget проходит резервное копирование, сборку и тесты."
    $BackupPath = New-PreUpdateBackup -Note "Safe update $shortOld -> $shortTarget"

    if (Test-ImageExists "tmod-discord-bot:latest") {
        $RollbackImage = "tmod-discord-bot:rollback-$shortOld"
        Invoke-Native docker tag tmod-discord-bot:latest $RollbackImage
    }
    if (Test-ImageExists "tmod-minecraft-supervisor:latest") {
        $RollbackSupervisorImage = "tmod-minecraft-supervisor:rollback-$shortOld"
        Invoke-Native docker tag tmod-minecraft-supervisor:latest $RollbackSupervisorImage
    }

    $CandidateDir = Join-Path $UpdateRoot "candidate-$shortTarget-$(Get-Date -Format yyyyMMddHHmmss)"
    Invoke-Native git -C $ProjectDir worktree add --detach $CandidateDir $TargetCommit
    Push-Location $CandidateDir
    try {
        Invoke-Native docker compose config --quiet
        Invoke-Native docker compose build
        Invoke-Native docker run --rm --entrypoint python tmod-discord-bot:latest -m unittest discover -s tests -p "test_*.py"
        $hostBackupPath = Resolve-HostBackupPath $BackupPath
        if (-not (Test-Path -LiteralPath $hostBackupPath)) {
            throw "Pre-update database backup is unavailable for migration validation"
        }
        $CandidateDbDir = Join-Path $UpdateRoot "db-validation-$shortTarget-$(Get-Date -Format yyyyMMddHHmmss)"
        New-Item -ItemType Directory -Path (Join-Path $CandidateDbDir "data") -Force | Out-Null
        Copy-Item -LiteralPath $hostBackupPath -Destination (Join-Path $CandidateDbDir "data\tmod.db") -Force
        Invoke-Native docker run --rm --user 0:0 --entrypoint python `
            --env "DATA_DIR=/app/persistent/data" `
            --env "DATABASE_FILE=/app/persistent/data/tmod.db" `
            --mount "type=bind,source=$CandidateDbDir,target=/app/persistent" `
            tmod-discord-bot:latest `
            -c "import sqlite3, storage; storage.init_db(); con=sqlite3.connect(storage.DATABASE_FILE); result=con.execute('PRAGMA integrity_check').fetchone()[0]; con.close(); assert result == 'ok', result"
        Invoke-Native docker run --rm `
            --mount "type=bind,source=$CandidateDir\Caddyfile,target=/etc/caddy/Caddyfile,readonly" `
            caddy:2.10.2-alpine caddy validate --config /etc/caddy/Caddyfile
    }
    finally { Pop-Location }

    Invoke-Native git -C $ProjectDir merge --ff-only "$Remote/$Branch"
    Write-UpdateStatus -State "deploying" -Message "Тесты пройдены; выполняется контролируемое переключение на $shortTarget."
    $runtimeCode = Invoke-CurrentRuntime -Directory $ProjectDir -SkipBuild $true
    if ($runtimeCode -ne 0) { throw "New release failed its post-deploy health check" }

    Write-UpdateStatus -State "success" -Message "Релиз $shortTarget проверен и успешно запущен." -Extra @{
        backup_path = $BackupPath
        tests = "passed"
        runtime_commit = $TargetCommit
    }
    Write-Host "[SAFE UPDATE] Release $shortTarget is healthy."
    exit 0
}
catch {
    $failure = "{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
    Write-Host "[SAFE UPDATE] Candidate rejected: $failure" -ForegroundColor Red
    try {
        if ($RollbackImage) { Invoke-Native docker tag $RollbackImage tmod-discord-bot:latest }
        if ($RollbackSupervisorImage) { Invoke-Native docker tag $RollbackSupervisorImage tmod-minecraft-supervisor:latest }
        if ($OldCommit -ne "unknown") { Restore-CodeRevision $OldCommit }
        $databaseRestored = Restore-DatabaseIfCorrupt
        $rollbackCode = Invoke-CurrentRuntime -Directory $ProjectDir -SkipBuild $true
        if ($rollbackCode -ne 0) { throw "Previous release could not be restarted" }
        Write-UpdateStatus -State "rolled_back" -Message "Новый релиз отклонён; предыдущая версия автоматически восстановлена." -Extra @{
            error = $failure
            backup_path = $BackupPath
            database_restored = $databaseRestored
            runtime_commit = $OldCommit
        }
        exit 0
    }
    catch {
        $rollbackFailure = "{0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message
        Write-UpdateStatus -State "failed" -Message "Не удалось завершить автоматический откат." -Extra @{
            error = $failure
            rollback_error = $rollbackFailure
            backup_path = $BackupPath
        }
        Write-Host "[SAFE UPDATE] Rollback failed: $rollbackFailure" -ForegroundColor Red
        exit 1
    }
}
finally {
    if ($CandidateDir -and (Test-Path -LiteralPath $CandidateDir)) {
        & git -C $ProjectDir worktree remove --force $CandidateDir *> $null
    }
    if ($CandidateDbDir -and (Test-Path -LiteralPath $CandidateDbDir)) {
        Remove-Item -LiteralPath $CandidateDbDir -Recurse -Force
    }
    & git -C $ProjectDir worktree prune *> $null
    if ($MutexAcquired) {
        $UpdateMutex.ReleaseMutex()
        $UpdateMutex.Dispose()
    }
}
