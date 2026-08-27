param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [string]$PersistentDir = "C:\Users\Admin\Documents\SGLDiscordBot",
    [string]$Branch = "main",
    [string]$Remote = "origin",
    [ValidateRange(15, 600)][int]$GitTimeoutSeconds = 90,
    [ValidateRange(60, 3600)][int]$BackupTimeoutSeconds = 600
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

function Invoke-GitFetchBounded {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$RemoteName,
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $process.StartInfo.FileName = "git.exe"
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $safeDirectory = $Directory.Replace('"', '\"')
    $safeRemote = $RemoteName.Replace('"', '\"')
    $safeBranch = $BranchName.Replace('"', '\"')
    $process.StartInfo.Arguments = (
        '-C "{0}" -c credential.interactive=never -c http.lowSpeedLimit=1 ' +
        '-c http.lowSpeedTime=20 fetch --prune "{1}" ' +
        '"+refs/heads/{2}:refs/remotes/{1}/{2}"'
    ) -f $safeDirectory, $safeRemote, $safeBranch
    try {
        if (-not $process.Start()) { return 125 }
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            Write-Host "[SAFE UPDATE] Git fetch exceeded ${TimeoutSeconds}s; terminating it." -ForegroundColor Yellow
            & taskkill.exe /PID $process.Id /T /F *> $null
            $process.WaitForExit(5000) | Out-Null
            return 124
        }
        return $process.ExitCode
    }
    catch {
        Write-Host "[SAFE UPDATE] Git fetch could not start: $($_.Exception.Message)" -ForegroundColor Yellow
        return 125
    }
    finally { $process.Dispose() }
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
    Write-Host "[SAFE UPDATE] Creating a consistent database backup (limit ${BackupTimeoutSeconds}s) ..."
    # Once PostgreSQL migration is active, the SQLite file is an immutable
    # archive and must never satisfy the production backup gate. Use the
    # matching v17 tools already present in the database container so this
    # remains reliable even while upgrading an older application image.
    & docker inspect tmod-postgres *> $null
    if ($LASTEXITCODE -eq 0) {
        $migrationTable = (& docker exec tmod-postgres psql -U tmod -d tmod -tAc "SELECT CASE WHEN to_regclass('public.tmod_platform_migrations') IS NULL THEN 0 ELSE 1 END" 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Could not verify the active PostgreSQL database before update"
        }
        $migrationMarker = "0"
        if ($migrationTable -eq "1") {
            $migrationMarker = (& docker exec tmod-postgres psql -U tmod -d tmod -tAc "SELECT CASE WHEN EXISTS (SELECT 1 FROM tmod_platform_migrations WHERE key='sqlite-to-postgresql-v1') THEN 1 ELSE 0 END" 2>$null | Out-String).Trim()
            if ($LASTEXITCODE -ne 0) {
                throw "Could not verify the PostgreSQL migration marker before update"
            }
        }
        if ($migrationMarker -eq "1") {
            $backupDirectory = Join-Path $PersistentDir "backups\database"
            New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
            $created = [DateTime]::UtcNow
            $stamp = $created.ToString("yyyyMMddTHHmmssffffffZ")
            $name = "tmod-pre-update-$stamp.dump"
            $hostPath = Join-Path $backupDirectory $name
            $containerPath = "/tmp/$name"
            try {
                & docker exec tmod-postgres pg_dump -U tmod -d tmod --format=custom --compress=6 --no-owner --no-privileges --file=$containerPath
                if ($LASTEXITCODE -ne 0) { throw "PostgreSQL pre-update pg_dump failed" }
                & docker exec tmod-postgres pg_restore --list $containerPath *> $null
                if ($LASTEXITCODE -ne 0) { throw "PostgreSQL pre-update dump validation failed" }
                & docker cp "tmod-postgres:$containerPath" $hostPath
                if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $hostPath)) { throw "PostgreSQL pre-update dump copy failed" }
                $size = (Get-Item -LiteralPath $hostPath).Length
                if ($size -lt 1024) { throw "PostgreSQL pre-update dump is unexpectedly small" }
                $metadata = [ordered]@{
                    name = $name
                    path = "/app/persistent/backups/database/$name"
                    kind = "pre-update"
                    backend = "postgresql"
                    created_at = $created.ToString("o")
                    size_bytes = $size
                    integrity = [ordered]@{ ok = $true; result = "pg_restore_list_ok" }
                    note = $Note
                } | ConvertTo-Json -Depth 5
                $metadataPath = [IO.Path]::ChangeExtension($hostPath, ".json")
                $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
                [IO.File]::WriteAllText($metadataPath, $metadata, $utf8NoBom)
                return "/app/persistent/backups/database/$name"
            }
            finally {
                & docker exec tmod-postgres rm -f $containerPath *> $null
            }
        }
    }
    & docker inspect tmod-discord-bot *> $null
    if ($LASTEXITCODE -eq 0) {
        $output = & docker exec tmod-discord-bot python /app/scripts/tmod_db_guard.py backup --kind pre-update --note $Note --timeout-seconds $BackupTimeoutSeconds 2>&1
        if ($LASTEXITCODE -ne 0 -and ($output | Out-String) -match "unrecognized arguments:.*timeout-seconds") {
            # One-release compatibility path: the running image may predate the
            # bounded CLI flag. The outer launch guard still supplies a hard cap.
            $output = & docker exec tmod-discord-bot python /app/scripts/tmod_db_guard.py backup --kind pre-update --note $Note 2>&1
        }
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
            python /app/scripts/tmod_db_guard.py backup --kind pre-update --note $Note --timeout-seconds $BackupTimeoutSeconds 2>&1
        if ($LASTEXITCODE -ne 0 -and ($output | Out-String) -match "unrecognized arguments:.*timeout-seconds") {
            $output = & docker run --rm --user 0:0 `
                --env "DATA_DIR=/app/persistent/data" `
                --env "DATABASE_FILE=/app/persistent/data/tmod.db" `
                --env "TMOD_DB_BACKUP_DIR=/app/persistent/backups/database" `
                --mount "type=bind,source=$PersistentDir,target=/app/persistent" `
                tmod-discord-bot:latest `
                python /app/scripts/tmod_db_guard.py backup --kind pre-update --note $Note 2>&1
        }
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
        $output = & py -3 (Join-Path $ProjectDir "scripts\tmod_db_guard.py") backup --kind pre-update --note $Note --timeout-seconds $BackupTimeoutSeconds 2>&1
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
    if ([IO.Path]::GetExtension($BackupPath) -eq ".dump") {
        & docker exec tmod-discord-bot python /app/scripts/tmod_db_guard.py check --full *> $null
        if ($LASTEXITCODE -eq 0) { return $false }
        Push-Location $ProjectDir
        try {
            & docker compose stop tmod-web tmod-worker tmod-discord-bot *> $null
            & docker compose run --rm --no-deps tmod-worker `
                python /app/scripts/tmod_db_guard.py restore $containerBackupPath --offline-confirmed
            if ($LASTEXITCODE -ne 0) { throw "PostgreSQL restore failed" }
        }
        finally { Pop-Location }
        return $true
    }
    Push-Location $ProjectDir
    try { & docker compose stop tmod-web tmod-worker tmod-discord-bot *> $null }
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

    Write-Host "[SAFE UPDATE] Fetching $Remote/$Branch (non-interactive, ${GitTimeoutSeconds}s hard limit) ..."
    $previousPrompt = $env:GIT_TERMINAL_PROMPT
    $env:GIT_TERMINAL_PROMPT = "0"
    try {
        $fetchExitCode = Invoke-GitFetchBounded `
            -Directory $ProjectDir `
            -RemoteName $Remote `
            -BranchName $Branch `
            -TimeoutSeconds $GitTimeoutSeconds
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
        if ([IO.Path]::GetExtension($hostBackupPath) -eq ".dump") {
            $relativeBackup = $hostBackupPath.Substring($PersistentDir.Length).TrimStart("\", "/").Replace("\", "/")
            Invoke-Native docker run --rm --user 0:0 --entrypoint pg_restore `
                --mount "type=bind,source=$PersistentDir,target=/app/persistent" `
                tmod-discord-bot:latest `
                --list "/app/persistent/$relativeBackup"
        }
        else {
            $CandidateDbDir = Join-Path $UpdateRoot "db-validation-$shortTarget-$(Get-Date -Format yyyyMMddHHmmss)"
            New-Item -ItemType Directory -Path (Join-Path $CandidateDbDir "data") -Force | Out-Null
            Copy-Item -LiteralPath $hostBackupPath -Destination (Join-Path $CandidateDbDir "data\tmod.db") -Force
            Invoke-Native docker run --rm --user 0:0 --entrypoint python `
                --env "DATA_DIR=/app/persistent/data" `
                --env "DATABASE_FILE=/app/persistent/data/tmod.db" `
                --mount "type=bind,source=$CandidateDbDir,target=/app/persistent" `
                tmod-discord-bot:latest `
                -c "import sqlite3, storage; storage.init_db(); con=sqlite3.connect(storage.DATABASE_FILE); result=con.execute('PRAGMA integrity_check').fetchone()[0]; con.close(); assert result == 'ok', result"
        }
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
        # If there was no known-good image before the update, rebuild the restored
        # old commit instead of ever starting an untested candidate image.
        $rollbackCode = Invoke-CurrentRuntime -Directory $ProjectDir -SkipBuild ([bool]$RollbackImage)
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
