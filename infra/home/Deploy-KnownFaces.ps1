#Requires -Version 7.2
[CmdletBinding()]
param(
    [ValidateSet('New','Existing')] [string] $Mode = 'Existing',
    [ValidateSet('Install','Update','Policy','Connect','Disconnect')] [string] $Action = 'Install',
    [Parameter(Mandatory)] [ValidatePattern('^[0-9a-f]{40}$')] [string] $Release,
    [ValidatePattern('^[a-z][a-z0-9-]{0,30}$')] [string] $VMName = 'knownfaces',
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')] [string] $SshHost,
    [ValidatePattern('^[a-z_][a-z0-9_-]*$')] [string] $SshUser = 'bridge',
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.-]*$')] [string] $HostKeyAlias,
    [string] $SwitchName,
    [string] $SshPublicKeyPath,
    [string] $PolicyPath,
    [guid] $SubscriptionId,
    [ValidatePattern('^[a-zA-Z0-9_-]+$')] [string] $ResourceGroup,
    [ValidatePattern('^[a-zA-Z0-9-]+$')] [string] $RelayNamespace,
    [switch] $Apply
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot/../..").Path
if ($Mode -eq 'Existing' -and !$SshHost) { throw 'Supply the existing guest SSH host or its trusted SSH alias.' }
if ($Action -in 'Install','Policy' -and (!$PolicyPath -or !(Test-Path -LiteralPath $PolicyPath -PathType Leaf))) { throw 'Supply the private KnownFaces policy JSON.' }
if ($Mode -eq 'New' -and (!$SwitchName -or !$SshPublicKeyPath)) { throw 'Supply the Hyper-V switch and operator SSH public key for dedicated mode.' }
if ($Action -eq 'Connect' -and (!$RelayNamespace -or !$ResourceGroup -or !$SubscriptionId)) { throw 'Connect needs SubscriptionId, ResourceGroup and RelayNamespace.' }
Write-Host "$Action KnownFaces at commit $Release using $Mode guest ($VMName $SshHost)."
if (!$Apply) { Write-Host 'Preview only. Add -Apply to run.'; return }
function Run([string] $Exe,[string[]] $Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed ($LASTEXITCODE)" }
}
$null = Get-Command git,ssh,scp -CommandType Application
Run git @('-C',$root,'cat-file','-e',"${Release}:infra/home/install.sh")
$sshOptions = @('-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=12')
if ($HostKeyAlias) { $sshOptions += @('-o',"HostKeyAlias=$HostKeyAlias") }
if ($Mode -eq 'New') {
    $guestInfo = & "$PSScriptRoot/New-KnownFacesGuest.ps1" -VMName $VMName -SwitchName $SwitchName -SshPublicKeyPath $SshPublicKeyPath -Create:($Action -eq 'Install')
    $SshHost = $guestInfo.Address
    $SshUser = 'knownfaces'
    $trusted = $guestInfo.KnownHosts.Replace('\','/')
    $sshOptions += @('-o',('UserKnownHostsFile="'+$trusted+'"'),'-o',"HostKeyAlias=$($guestInfo.Alias)",'-o','ConnectionAttempts=10')
    if ($SshPublicKeyPath.EndsWith('.pub')) {
        $privateKey = $SshPublicKeyPath.Substring(0,$SshPublicKeyPath.Length-4)
        if (Test-Path -LiteralPath $privateKey) { $sshOptions += @('-i',$privateKey) }
    }
}
$login = "$SshUser@$SshHost"
function Guest([string[]] $Arguments) { Run ssh ($sshOptions + $login + $Arguments) }
function Upload([string] $Local,[string] $Remote) { Run scp ($sshOptions + $Local + "${login}:$Remote") }
$temp = New-TemporaryFile
try {
    Run git @('-C',$root,'archive','--format=tar',"--output=$($temp.FullName)",$Release)
    $stage = (Guest @('mktemp','-d','/tmp/knownfaces.XXXXXXXX')).Trim()
    if ($stage -notmatch '^/tmp/knownfaces\.[A-Za-z0-9]{8}$') { throw 'Unexpected guest staging path' }
    try {
        Upload $temp.FullName "$stage/source.tar"
        Guest @('tar','-xf',"$stage/source.tar",'-C',$stage,'infra/home/install.sh')
        if ($Action -in 'Install','Policy') { Upload (Resolve-Path -LiteralPath $PolicyPath).Path "$stage/policy.json" }
        $install = @('sudo','-n','bash',"$stage/infra/home/install.sh",$Action,$Release,$stage)
        if ($Action -eq 'Connect') {
            # Retrieve on the home host only. No secret in command arguments, files or logs.
            $key = & az relay hyco authorization-rule keys list --subscription $SubscriptionId -g $ResourceGroup --namespace-name $RelayNamespace --hybrid-connection-name mcp --name home-listen --query primaryConnectionString -o tsv --only-show-errors
            if ($LASTEXITCODE -ne 0 -or !$key -or $key -notmatch [regex]::Escape("$RelayNamespace.servicebus.windows.net")) { throw 'Cannot read the scoped home-listen connection string' }
            try {
                $key | ssh @sshOptions $login @install
                if ($LASTEXITCODE -ne 0) { throw 'Listener configuration failed' }
            } finally { Remove-Variable key }
        } else { Guest $install }
    } finally { Guest @('rm','-rf','--',$stage) }
} finally { Remove-Item -LiteralPath $temp.FullName }
