param(
    [Parameter(Mandatory = $true)]
    [string]$RepoPath
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path -LiteralPath $RepoPath).Path
$gitDir = Join-Path $repo ".git"
$pidFile = Join-Path $gitDir "auto-update-watcher.pid"
$logFile = Join-Path $gitDir "auto_update.log"

function Write-UpdateLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -LiteralPath $logFile -Value $line
}

if (-not (Test-Path -LiteralPath (Join-Path $gitDir "HEAD"))) {
    throw "Auto-update watcher requires a Git repository: $repo"
}

if (Test-Path -LiteralPath $pidFile) {
    $existingPid = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($existingPid -match '^\d+$' -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        exit 0
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

Set-Content -LiteralPath $pidFile -Value $PID
Write-UpdateLog "Watcher started."

try {
    while ($true) {
        Push-Location $repo
        try {
            $changes = @(git status --porcelain)
            if ($changes.Count -gt 0) {
                Write-UpdateLog "Changes detected; waiting 20 seconds for edits to settle."
                Start-Sleep -Seconds 20

                $changes = @(git status --porcelain)
                if ($changes.Count -gt 0) {
                    git add -A
                    if ($LASTEXITCODE -ne 0) {
                        throw "git add failed."
                    }

                    git diff --cached --quiet
                    if ($LASTEXITCODE -eq 1) {
                        git commit -m "Auto-update"
                        if ($LASTEXITCODE -ne 0) {
                            throw "git commit failed."
                        }
                        Write-UpdateLog "Committed local changes."
                    } elseif ($LASTEXITCODE -ne 0) {
                        throw "Unable to inspect staged changes."
                    }
                }
            }

            $ahead = @(git log "@{u}..HEAD" --oneline)
            if ($ahead.Count -gt 0) {
                git push --quiet
                if ($LASTEXITCODE -ne 0) {
                    Write-UpdateLog "Push was rejected; fetching and rebasing once."
                    git fetch --quiet
                    if ($LASTEXITCODE -ne 0) {
                        throw "git fetch failed after a rejected push."
                    }
                    git rebase "@{u}" --quiet
                    if ($LASTEXITCODE -ne 0) {
                        git rebase --abort *> $null
                        throw "git rebase failed after a rejected push."
                    }
                    git push --quiet
                    if ($LASTEXITCODE -ne 0) {
                        throw "git push failed after rebasing."
                    }
                }
                Write-UpdateLog "Pushed $($ahead.Count) commit(s)."
            }
        } catch {
            Write-UpdateLog "Update failed: $($_.Exception.Message)"
        } finally {
            Pop-Location
        }

        Start-Sleep -Seconds 10
    }
} finally {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-UpdateLog "Watcher stopped."
}
