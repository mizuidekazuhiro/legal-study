param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repo '.venv\Scripts\python.exe'
$settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$logRoot = [string]$settings.service_directory
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$logPath = Join-Path $logRoot 'launcher.log'
$created = $false
$mutexKey = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($settings.home.ToLowerInvariant()))).Replace('-','')
$mutex = New-Object Threading.Mutex($false, ('Local\LegalStudyLauncher-' + $mutexKey))
try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
if (!$acquired) { $mutex.Dispose(); exit 0 }
function Write-LauncherLog([string]$Message) {
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
        Move-Item -LiteralPath $logPath -Destination ($logPath + '.previous') -Force
    }
    Add-Content -LiteralPath $logPath -Value ((Get-Date).ToString('o') + ' ' + $Message) -Encoding UTF8
}
$exitCode = 1
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public sealed class LegalStudyChildJob : IDisposable {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int kind, IntPtr value, uint size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    IntPtr handle;
    public LegalStudyChildJob() {
        if (IntPtr.Size != 8) throw new PlatformNotSupportedException("64-bit Windows required");
        handle=CreateJobObject(IntPtr.Zero,null);
        IntPtr value=Marshal.AllocHGlobal(144);
        try {
            Marshal.Copy(new byte[144],0,value,144);
            Marshal.WriteInt32(value,16,0x2000);
            if (handle==IntPtr.Zero || !SetInformationJobObject(handle,9,value,144)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        } finally { Marshal.FreeHGlobal(value); }
    }
    public void Attach(IntPtr process) {
        if (!AssignProcessToJobObject(handle,process)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
    public void Dispose() { if(handle!=IntPtr.Zero) { CloseHandle(handle);handle=IntPtr.Zero; } }
}
'@
$job = New-Object LegalStudyChildJob
try {
    Push-Location -LiteralPath $repo
    for ($attempt = 0; $attempt -le 3; $attempt++) {
        Write-LauncherLog ('START attempt=' + $attempt)
        $env:PYTHONUTF8 = '1'
        # The outer Job also owns the venv launcher and supervisor, so Task Stop
        # closes the entire owned process tree rather than orphaning Python.
        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = $python
        $startInfo.Arguments = '-u -m legal_study.automation.watch_service --config "' + $Config + '"'
        $startInfo.WorkingDirectory = $repo
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $child = [Diagnostics.Process]::Start($startInfo)
        try { $job.Attach($child.Handle) } catch { $child.Kill(); throw }
        $stdout = $child.StandardOutput.ReadToEndAsync()
        $stderr = $child.StandardError.ReadToEndAsync()
        $child.WaitForExit()
        $exitCode = $child.ExitCode
        foreach ($text in @($stdout.Result,$stderr.Result)) {
            if ($text) { Write-LauncherLog ($text.Substring(0,[Math]::Min(16384,$text.Length))) }
        }
        $child.Dispose()
        Write-LauncherLog ('EXIT code=' + $exitCode)
        if ($exitCode -eq 0 -or $attempt -eq 3) { break }
        Write-LauncherLog 'RETRY after 60 seconds; abnormal exit only'
        Start-Sleep -Seconds 60
    }
} catch {
    Write-LauncherLog ('ERROR ' + $_.Exception.Message)
    $exitCode = 1
} finally {
    $job.Dispose()
    Pop-Location
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
exit $exitCode
