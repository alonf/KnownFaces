#Requires -Version 7.2
# Called by Deploy-KnownFaces on the Windows Hyper-V host. The seed contains only public keys.
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[a-z][a-z0-9-]{0,30}$')] [string] $VMName,
    [Parameter(Mandatory)] [string] $SwitchName,
    [Parameter(Mandatory)] [string] $SshPublicKeyPath,
    [switch] $Create
)
$ErrorActionPreference = 'Stop'
$lock = [Threading.Mutex]::new($false,"Global\KnownFaces-guest-$VMName")
try { $locked = $lock.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
if (!$locked) { $lock.Dispose(); throw 'Another KnownFaces operation is preparing this VM' }
try {
$parent = (Get-VMHost).VirtualMachinePath
$folder = Join-Path $parent $VMName
$marker = Join-Path $folder 'knownfaces-guest-v1'
$trust = Join-Path $folder 'knownfaces-known-hosts'
$disk = Join-Path $folder 'system.vhdx'
$seed = Join-Path $folder 'cidata.vhdx'
$aliasName = "kf-$VMName"
$key = (Get-Content -LiteralPath $SshPublicKeyPath -Raw).Trim()
if ($key -notmatch '^(ssh-ed25519|ssh-rsa) ([A-Za-z0-9+/]+={0,3})(?: [^\r\n]*)?$') { throw 'Expected an OpenSSH public key.' }
$key = "$($Matches[1]) $($Matches[2])"
$vm = Get-VM -Name $VMName -ErrorAction SilentlyContinue
if ($vm -and (!(Test-Path -LiteralPath $marker) -or $vm.Notes -cne "KnownFaces:$VMName")) {
    throw 'VM belongs to another installation. Use Existing mode to install through trusted SSH.'
}
function With-Seed([bool] $ReadOnly, [scriptblock] $Body) {
    $mount = Join-Path ([IO.Path]::GetTempPath()) ("kf-seed-"+[guid]::NewGuid())
    $mount = (New-Item -ItemType Directory -Path $mount).FullName
    try {
        $volume = Mount-VHD -Path $seed -ReadOnly:$ReadOnly -Passthru
        try {
            if ($ReadOnly) { $part = Get-Partition -DiskNumber $volume.DiskNumber | Where-Object Type -ne Reserved | Select-Object -Last 1 }
            else {
                Initialize-Disk -Number $volume.DiskNumber -PartitionStyle MBR
                $part = New-Partition -DiskNumber $volume.DiskNumber -UseMaximumSize
                Format-Volume -Partition $part -FileSystem FAT32 -NewFileSystemLabel CIDATA -Confirm:$false | Out-Null
            }
            Add-PartitionAccessPath -DiskNumber $volume.DiskNumber -PartitionNumber $part.PartitionNumber -AccessPath $mount
            try { & $Body $mount }
            finally { Remove-PartitionAccessPath -DiskNumber $volume.DiskNumber -PartitionNumber $part.PartitionNumber -AccessPath $mount }
        } finally { Dismount-VHD -Path $seed }
    } finally { [IO.Directory]::Delete($mount) }
}
if (!$vm) {
    if (!$Create) { throw 'Dedicated VM is missing. Run Install first.' }
    $null = Get-VMSwitch -Name $SwitchName
    if (Test-Path -LiteralPath $folder) {
        if (!(Test-Path -LiteralPath $marker)) { throw 'Refusing to use an existing unowned VM folder' }
    } else {
        $null = New-Item -ItemType Directory -Path $folder
        [IO.File]::WriteAllText($marker,$VMName)
    }
    $url = 'https://cloud-images.ubuntu.com/noble/current'
    $name = 'noble-server-cloudimg-amd64.img'
    if (!(Test-Path -LiteralPath $disk)) {
        $sums = (Invoke-WebRequest "$url/SHA256SUMS").Content
        if ($sums -is [byte[]]) { $sums=[Text.Encoding]::UTF8.GetString($sums) }
        if ($sums -notmatch "(?m)^([0-9a-f]{64})\s+\*?$([regex]::Escape($name))\s*$") { throw 'Canonical checksum unavailable' }
        $expected = $Matches[1]
        $image = Join-Path $folder $name
        if (!(Test-Path -LiteralPath $image) -or (Get-FileHash $image).Hash -ne $expected) {
            Invoke-WebRequest "$url/$name" -OutFile $image
        }
        if ((Get-FileHash $image).Hash -ne $expected) { throw 'Ubuntu image checksum mismatch' }
        $fixed = Join-Path $folder ("conversion-"+[guid]::NewGuid()+".vhd")
        $dynamic = Join-Path $folder ("conversion-"+[guid]::NewGuid()+".vhdx")
        try {
            & "$PSScriptRoot/convert-qcow2.ps1" -Path $image -Destination $fixed | Out-Host
            Convert-VHD -Path $fixed -DestinationPath $dynamic -VHDType Dynamic
            Resize-VHD -Path $dynamic -SizeBytes 40GB
            Move-Item -LiteralPath $dynamic -Destination $disk
        } finally {
            foreach ($file in @($fixed,$dynamic)) { if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file } }
        }
    }
    if (!(Test-Path -LiteralPath "$folder/seed-ready")) {
        if (Test-Path -LiteralPath $seed) { Remove-Item -LiteralPath $seed }
        New-VHD -Path $seed -Dynamic -SizeBytes 64MB | Out-Null
        $userData = @"
#cloud-config
hostname: $VMName
users:
  - name: knownfaces
    shell: /bin/bash
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    lock_passwd: true
    ssh_authorized_keys:
      - '$key'
ssh_pwauth: false
disable_root: true
packages: [linux-cloud-tools-virtual, docker.io, docker-compose-v2]
runcmd:
  - [mkdir, -p, /run/kf-seed]
  - [mount, -L, CIDATA, /run/kf-seed]
  - [cp, /etc/ssh/ssh_host_ed25519_key.pub, /run/kf-seed/guest.pub]
  - [umount, /run/kf-seed]
  - [touch, /etc/cloud/cloud-init.disabled]
power_state: {mode: poweroff, delay: now, timeout: 30}
"@
        With-Seed $false { param($dir)
            [IO.File]::WriteAllText("$dir/user-data", $userData.Replace("`r","")+"`n")
            [IO.File]::WriteAllText("$dir/meta-data","instance-id: knownfaces-$VMName`nlocal-hostname: $VMName`n")
        }
        [IO.File]::WriteAllText("$folder/seed-ready",$VMName)
    }
    New-VM -Name $VMName -Generation 2 -SwitchName $SwitchName -VHDPath $disk -MemoryStartupBytes 6GB -Path $parent | Out-Null
    Set-VM -Name $VMName -Notes "KnownFaces:$VMName"
}
if (!(Test-Path -LiteralPath $trust)) {
    if ((Get-VM $VMName).State -eq 'Off') {
        $pub = $null
        if (Get-VMHardDiskDrive $VMName | Where-Object Path -eq $seed) {
            Get-VMHardDiskDrive $VMName | Where-Object Path -eq $seed | Remove-VMHardDiskDrive
        }
        With-Seed $true { param($dir)
            if (Test-Path -LiteralPath "$dir/guest.pub") { Get-Content -LiteralPath "$dir/guest.pub" }
        } | ForEach-Object { $pub = $_ }
        if (!$pub) {
            Set-VM -Name $VMName -ProcessorCount 4 -CheckpointType Disabled -AutomaticStartAction Start -AutomaticStopAction ShutDown
            Set-VMFirmware -VMName $VMName -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority
            Add-VMHardDiskDrive -VMName $VMName -Path $seed
            Start-VM $VMName
        }
    }
    $deadline=(Get-Date).AddMinutes(30)
    while ((Get-VM $VMName).State -ne 'Off' -and (Get-Date) -lt $deadline) { Start-Sleep 5 }
    if ((Get-VM $VMName).State -ne 'Off') { throw 'Cloud-init has not completed its first-boot shutdown. Retry Install after it finishes.' }
    Get-VMHardDiskDrive $VMName | Where-Object Path -eq $seed | Remove-VMHardDiskDrive
    $pub = With-Seed $true { param($dir) Get-Content -LiteralPath "$dir/guest.pub" }
    if ($pub -notmatch '^ssh-ed25519 ([a-zA-Z0-9+/]+={0,3})(?: .*)?$') { throw 'Missing trusted guest SSH host key' }
    [IO.File]::WriteAllText($trust,"$aliasName ssh-ed25519 $($Matches[1])`n")
}
if ((Get-VM $VMName).State -eq 'Off') { Start-VM $VMName }
$deadline=(Get-Date).AddMinutes(5)
do {
    $ips = @((Get-VMNetworkAdapter $VMName).IPAddresses | Where-Object { $_ -match '^(?!127\.|169\.254\.)\d+(\.\d+){3}$' } | Sort-Object -Unique)
    if ($ips.Count -eq 1) { break }
    Start-Sleep 3
} while ((Get-Date) -lt $deadline)
if ($ips.Count -ne 1) { throw 'Guest must report one usable IPv4 address' }
[pscustomobject]@{ Address=$ips[0]; KnownHosts=$trust; Alias=$aliasName }
} finally {
    $lock.ReleaseMutex()
    $lock.Dispose()
}
