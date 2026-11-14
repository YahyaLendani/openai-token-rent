# Installs (or upgrades) the openai-token-rent skill for every agent found on this machine:
#   Codex CLI   -> ~\.agents\skills\openai-token-rent
#   Claude Code -> ~\.claude\skills\openai-token-rent
# Usage (PowerShell):  irm https://raw.githubusercontent.com/YahyaLendani/openai-token-rent/main/install.ps1 | iex
# It downloads the latest release from GitHub. It uploads nothing and touches only those folders.
& {
    $ErrorActionPreference = 'Stop'
    $Repo = 'YahyaLendani/openai-token-rent'
    $Name = 'openai-token-rent'
    $HomeDir = if ($env:TOKEN_RENT_HOME) { $env:TOKEN_RENT_HOME } else { $HOME }   # override for testing
    $Src = $env:TOKEN_RENT_SRC                                                       # local .zip (testing/offline)

    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('token-rent-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        if (-not $Src) {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $ref = 'refs/heads/main'
            try {
                $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ 'User-Agent' = 'token-rent-installer' }
                if ($rel.tag_name) { $ref = "refs/tags/$($rel.tag_name)" }
            } catch { }
            Write-Host "token-rent: downloading $Repo ($ref)"
            $Src = Join-Path $tmp 'src.zip'
            Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/$ref" -OutFile $Src -UseBasicParsing
        }

        $x = Join-Path $tmp 'x'
        Expand-Archive -Path $Src -DestinationPath $x -Force
        $srcDir = Get-ChildItem -Path $x -Directory | Select-Object -First 1
        if (-not $srcDir -or -not (Test-Path (Join-Path $srcDir.FullName 'SKILL.md'))) {
            throw 'the download does not contain SKILL.md; nothing installed'
        }
        foreach ($dev in 'evals', '.gitattributes', '.gitignore') {
            $p = Join-Path $srcDir.FullName $dev
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }

        $targets = @()
        if ((Test-Path (Join-Path $HomeDir '.codex')) -or (Get-Command codex -ErrorAction SilentlyContinue)) {
            $targets += (Join-Path $HomeDir '.agents\skills')
        }
        if ((Test-Path (Join-Path $HomeDir '.claude')) -or (Get-Command claude -ErrorAction SilentlyContinue)) {
            $targets += (Join-Path $HomeDir '.claude\skills')
        }
        if ($targets.Count -eq 0) {
            $targets += (Join-Path $HomeDir '.agents\skills')
            Write-Host 'token-rent: no Codex or Claude Code found; installing to the standard ~\.agents\skills'
        }

        foreach ($t in $targets) {
            $dest = Join-Path $t $Name
            if (Test-Path (Join-Path $dest '.git')) {
                Write-Host "token-rent: $dest is a git clone; leaving it alone (run git pull there to upgrade)"
                continue
            }
            New-Item -ItemType Directory -Force -Path $t | Out-Null
            $staged = "$dest.new"
            if (Test-Path $staged) { Remove-Item -Recurse -Force $staged }
            Copy-Item -Recurse -Path $srcDir.FullName -Destination $staged
            if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
            Move-Item -Path $staged -Destination $dest
            Write-Host "token-rent: installed $dest"
        }
        Write-Host 'token-rent: done. Ask your agent about OpenAI costs; it loads the skill by itself.'
        Write-Host '            (If it does not show up, restart Codex or Claude Code once.)'
    }
    catch {
        Write-Host "token-rent: $($_.Exception.Message)" -ForegroundColor Red
    }
    finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}
