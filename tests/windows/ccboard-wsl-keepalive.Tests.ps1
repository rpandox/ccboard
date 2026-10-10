# Pester 5 tests for scripts/windows/ccboard-wsl-keepalive.ps1 (issue #123; the script is issue #118).
#
# Nothing here touches the machine it runs on: Register-ScheduledTask, Unregister-ScheduledTask, Start-ScheduledTask, Stop-ScheduledTask and
# Get-ScheduledTask are mocked, wsl.exe is a function that records its arguments, and USERPROFILE points at Pester's TestDrive, so no scheduled
# task is created and the real .wslconfig is never read or written. The ScheduledTasks module of Windows still builds the task objects
# (New-ScheduledTaskAction and friends only create objects in memory), which is why the tests are skipped off Windows.
#
# Run it on a Windows machine with Pester 5:  Invoke-Pester -Path tests/windows

$onWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or ($IsWindows -eq $true)

Describe 'ccboard-wsl-keepalive.ps1' -Skip:(-not $onWindows) {
  BeforeAll {
    $script:KeepAlive = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\scripts\windows\ccboard-wsl-keepalive.ps1'))
    $script:SavedProfile = $env:USERPROFILE
    # wsl.exe: one function that remembers how it was called and answers `-l -q` with the distros the test sets.
    function global:wsl.exe {
      $global:WslCalls += , ($args -join ' ')
      if ($global:WslDistros) { $global:WslDistros }
    }
  }

  AfterAll {
    $env:USERPROFILE = $script:SavedProfile
    Remove-Item -Path 'function:global:wsl.exe' -ErrorAction SilentlyContinue
    Remove-Variable -Name WslCalls, WslDistros -Scope Global -ErrorAction SilentlyContinue
  }

  BeforeEach {
    $global:WslCalls = @()
    $global:WslDistros = @('Ubuntu', 'Debian')
    $env:USERPROFILE = $TestDrive
    Mock Get-ScheduledTask { $null }
    Mock Register-ScheduledTask { }
    Mock Unregister-ScheduledTask { }
    Mock Start-ScheduledTask { }
    Mock Stop-ScheduledTask { }
  }

  Context 'registering the keep-alive task' {
    It 'registers one task that holds a hidden wsl.exe open for the named distro, then starts it' {
      $out = & $script:KeepAlive -Distro Debian 6>&1 | Out-String
      $LASTEXITCODE | Should -Be 0
      Should -Invoke Register-ScheduledTask -Times 1 -Exactly -ParameterFilter {
        $TaskName -eq 'ccboard-wsl-keepalive' -and $Action.Execute -eq 'powershell.exe' -and
        $Action.Arguments -like '*wsl.exe -d Debian --exec sleep infinity*' -and $Action.Arguments -like '*-WindowStyle Hidden*'
      }
      Should -Invoke Start-ScheduledTask -Times 1 -Exactly -ParameterFilter { $TaskName -eq 'ccboard-wsl-keepalive' }
      Should -Invoke Unregister-ScheduledTask -Times 0 -Exactly
      $out | Should -Match 'Registered the scheduled task ccboard-wsl-keepalive \(distro Debian, method sleep\)'
    }

    It 'runs for the current user without administrator rights' {
      & $script:KeepAlive -Distro Ubuntu 6>&1 | Out-Null
      Should -Invoke Register-ScheduledTask -Times 1 -Exactly -ParameterFilter { $Principal.RunLevel -eq 'Limited' }
    }

    It 'takes the first distro wsl.exe lists when none is named' {
      & $script:KeepAlive 6>&1 | Out-Null
      Should -Invoke Register-ScheduledTask -Times 1 -Exactly -ParameterFilter { $Action.Arguments -like '*wsl.exe -d Ubuntu --exec sleep infinity*' }
      $global:WslCalls | Should -Contain '-l -q'
    }

    It 'uses dbus-launch for -Method dbus' {
      & $script:KeepAlive -Distro Ubuntu -Method dbus 6>&1 | Out-Null
      Should -Invoke Register-ScheduledTask -Times 1 -Exactly -ParameterFilter { $Action.Arguments -like '*wsl.exe -d Ubuntu --exec dbus-launch true*' }
    }

    It 'replaces a task that is already there instead of adding a second one' {
      Mock Get-ScheduledTask { [pscustomobject]@{ TaskName = 'ccboard-wsl-keepalive' } }
      $out = & $script:KeepAlive -Distro Ubuntu 6>&1 | Out-String
      Should -Invoke Unregister-ScheduledTask -Times 1 -Exactly -ParameterFilter { $TaskName -eq 'ccboard-wsl-keepalive' }
      Should -Invoke Register-ScheduledTask -Times 1 -Exactly
      $out | Should -Match 'Replaced the scheduled task'
    }
  }

  Context 'when there is nothing to register' {
    It 'exits 0 and changes nothing for a distro that is not installed' {
      $out = & $script:KeepAlive -Distro Fedora 6>&1 | Out-String
      $LASTEXITCODE | Should -Be 0
      $out | Should -Match "No distro named 'Fedora' is installed"
      Should -Invoke Register-ScheduledTask -Times 0 -Exactly
      Should -Invoke Start-ScheduledTask -Times 0 -Exactly
    }

    It 'exits 0 and changes nothing when wsl.exe lists no distro' {
      $global:WslDistros = @()
      $out = & $script:KeepAlive 6>&1 | Out-String
      $LASTEXITCODE | Should -Be 0
      $out | Should -Match 'No WSL distro is installed'
      Should -Invoke Register-ScheduledTask -Times 0 -Exactly
    }

    It 'exits 0 and changes nothing when wsl.exe is not there' {
      Mock Get-Command { $null } -ParameterFilter { $Name -eq 'wsl.exe' }
      $out = & $script:KeepAlive 6>&1 | Out-String
      $LASTEXITCODE | Should -Be 0
      $out | Should -Match 'wsl.exe was not found'
      Should -Invoke Register-ScheduledTask -Times 0 -Exactly
      $global:WslCalls.Count | Should -Be 0
    }

    It 'refuses a distro name with characters outside letters, digits, dot, dash and underscore' {
      { & $script:KeepAlive -Distro 'Ubuntu; calc' 6>&1 | Out-Null } | Should -Throw
      Should -Invoke Register-ScheduledTask -Times 0 -Exactly
    }
  }

  Context '-Remove' {
    It 'unregisters an existing task and registers nothing' {
      Mock Get-ScheduledTask { [pscustomobject]@{ TaskName = 'ccboard-wsl-keepalive' } }
      $out = & $script:KeepAlive -Remove 6>&1 | Out-String
      $LASTEXITCODE | Should -Be 0
      Should -Invoke Stop-ScheduledTask -Times 1 -Exactly
      Should -Invoke Unregister-ScheduledTask -Times 1 -Exactly -ParameterFilter { $TaskName -eq 'ccboard-wsl-keepalive' }
      Should -Invoke Register-ScheduledTask -Times 0 -Exactly
      $out | Should -Match 'Removed the scheduled task ccboard-wsl-keepalive'
    }

    It 'says so when there is no task and removes nothing' {
      $out = & $script:KeepAlive -Remove 6>&1 | Out-String
      $LASTEXITCODE | Should -Be 0
      Should -Invoke Unregister-ScheduledTask -Times 0 -Exactly
      $out | Should -Match 'Nothing to remove'
    }

    It 'does not ask wsl.exe anything' {
      & $script:KeepAlive -Remove 6>&1 | Out-Null
      $global:WslCalls.Count | Should -Be 0
    }
  }

  Context '.wslconfig' {
    It 'is only printed, never written, without -WriteWslConfig' {
      $out = & $script:KeepAlive -Distro Ubuntu 6>&1 | Out-String
      Test-Path -LiteralPath (Join-Path $TestDrive '.wslconfig') | Should -BeFalse
      $out | Should -Match 'instanceIdleTimeout=-1'
      $out | Should -Match 'Nothing was written'
    }

    It 'is merged after a backup with -WriteWslConfig, keeping the settings already there' {
      $path = Join-Path $TestDrive '.wslconfig'
      Set-Content -LiteralPath $path -Value "[wsl2]`r`nmemory=4GB`r`n" -Encoding ASCII
      & $script:KeepAlive -Distro Ubuntu -WriteWslConfig 6>&1 | Out-Null
      $text = Get-Content -LiteralPath $path -Raw
      $text | Should -Match 'memory=4GB'
      $text | Should -Match '(?m)^vmIdleTimeout=-1'
      $text | Should -Match '(?m)^\[general\]'
      $text | Should -Match '(?m)^instanceIdleTimeout=-1'
      $text | Should -Not -Match 'networkingMode'
      @(Get-ChildItem -Path $TestDrive -Filter '.wslconfig.ccboard-bak-*').Count | Should -Be 1
    }

    It 'adds networkingMode=mirrored only with -Mirrored' {
      & $script:KeepAlive -Distro Ubuntu -WriteWslConfig -Mirrored 6>&1 | Out-Null
      Get-Content -LiteralPath (Join-Path $TestDrive '.wslconfig') -Raw | Should -Match '(?m)^networkingMode=mirrored'
    }

    It 'rewrites a value that is there instead of adding a second line' {
      $path = Join-Path $TestDrive '.wslconfig'
      Set-Content -LiteralPath $path -Value "[wsl2]`r`nvmIdleTimeout=60000`r`n" -Encoding ASCII
      & $script:KeepAlive -Distro Ubuntu -WriteWslConfig 6>&1 | Out-Null
      $text = Get-Content -LiteralPath $path -Raw
      ([regex]::Matches($text, 'vmIdleTimeout')).Count | Should -Be 1
      $text | Should -Match '(?m)^vmIdleTimeout=-1'
    }
  }
}
