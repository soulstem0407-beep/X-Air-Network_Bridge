# Official REAPER setup launcher (Windows).
# Linux: xair_network_bridge_reaper_setup.sh   macOS: xair_network_bridge_reaper_setup.command
#
# Writes project-local template + OSC surface. Does NOT change WASAPI/ASIO
# device selection, sample rate, buffers, HDMI, reaper.ini, or .env.
# GET-only OSC to the X18.
#
# Usage: powershell -ExecutionPolicy Bypass -File launchers/xair_network_bridge/xair_network_bridge_reaper_setup.ps1

$ErrorActionPreference = "Stop"
$Channels = 18
$OscPortDefault = 10024
$ReaperListen = 8000
$BridgeListen = 9001
$ClientName = "xair_net_bridge"

$Root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
Set-Location $Root

function Write-Ok($m) { Write-Host $m -ForegroundColor Green }
function Write-Wn($m) { Write-Host $m -ForegroundColor Yellow }
function Write-Er($m) { Write-Host $m -ForegroundColor Red }
function Die($m) { Write-Er "ERROR  $m"; exit 2 }

function Get-DotEnv($path) {
  $map = @{}
  if (-not (Test-Path $path)) { return $map }
  Get-Content -LiteralPath $path | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { return }
    $k = $line.Substring(0, $i).Trim()
    $v = $line.Substring($i + 1).Trim()
    if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
    $map[$k] = $v
  }
  return $map
}

function Cfg($envmap, $key, $def) {
  $fromEnv = [Environment]::GetEnvironmentVariable($key)
  if ($fromEnv) { return [string]$fromEnv }
  if ($envmap.ContainsKey($key) -and $envmap[$key]) { return [string]$envmap[$key] }
  return $def
}

function Sanitize([string]$s, [string]$fallback) {
  $t = -join ($s.ToCharArray() | Where-Object { $_ -match '[A-Za-z0-9_-]' })
  if ([string]::IsNullOrEmpty($t)) { return $fallback }
  if ($t.Length -gt 40) { $t = $t.Substring(0, 40) }
  return $t
}

function Get-OscPad([string]$s) {
  $b = [System.Text.Encoding]::ASCII.GetBytes($s)
  $total = $b.Length + 1
  $pad = (4 - ($total % 4)) % 4
  $out = New-Object byte[] ($total + $pad)
  [Array]::Copy($b, $out, $b.Length)
  return $out
}

function Send-OscQuery([string]$hostName, [int]$port, [string]$addr) {
  $msg = (Get-OscPad $addr) + (Get-OscPad ",")
  $udp = New-Object System.Net.Sockets.UdpClient
  try {
    $udp.Client.ReceiveTimeout = 2000
    $udp.Connect($hostName, $port)
    [void]$udp.Send($msg, $msg.Length)
    $ep = New-Object System.Net.IPEndPoint ([System.Net.IPAddress]::Any, 0)
    return $udp.Receive([ref]$ep)
  } catch {
    return $null
  } finally {
    $udp.Close()
  }
}

function Bytes-Has([byte[]]$data, [string]$ascii) {
  if ($null -eq $data) { return $false }
  $t = [System.Text.Encoding]::ASCII.GetString($data)
  return $t.Contains($ascii)
}

function Find-Reaper {
  $cands = @(
    (Join-Path ${env:ProgramFiles} "REAPER (x64)\reaper.exe"),
    (Join-Path ${env:ProgramFiles} "REAPER\reaper.exe")
  )
  if (${env:ProgramFiles(x86)}) {
    $cands += (Join-Path ${env:ProgramFiles(x86)} "REAPER\reaper.exe")
  }
  $cmd = Get-Command reaper -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  foreach ($p in $cands) { if (Test-Path $p) { return $p } }
  return $null
}

$envmap = Get-DotEnv (Join-Path $Root ".env")
$ClientName = Cfg $envmap "XAIR_JACK_CLIENT_NAME" $ClientName
$OscHost = Cfg $envmap "XAIR_OSC_HOST" ""
if (-not $OscHost) { $OscHost = Cfg $envmap "XAIR_OSC_IP" "" }
$OscPort = [int](Cfg $envmap "XAIR_OSC_PORT" "$OscPortDefault")
$ReaperListen = [int](Cfg $envmap "XAIR_REAPER_PORT" "$ReaperListen")
$BridgeListen = [int](Cfg $envmap "XAIR_REAPER_LISTEN_PORT" "$BridgeListen")
$Sr = [int](Cfg $envmap "XAIR_SAMPLE_RATE" "48000")

Write-Host "X-Air Network Bridge — REAPER setup (project files only)" -ForegroundColor Cyan
Write-Host "OS: Windows  audio backend: WASAPI/ASIO"
Write-Host "Does not change WASAPI/ASIO device, buffers, HDMI, or reaper.ini"

$reaper = Find-Reaper
if (-not $reaper) { Die "REAPER is not installed (Program Files\REAPER)" }
Write-Host "  REAPER: $reaper"

$portsOk = $false
$audio = @()
try { $audio = @(Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue) } catch { $audio = @() }
$hit = $audio | Where-Object { $_.Name -match 'VB-Audio|CABLE|Voicemeeter|ASIO|X-AIR|X18|XR18' }
if ($hit) {
  $portsOk = $true
  Write-Host ("  WASAPI/ASIO devices: " + (($hit | ForEach-Object { $_.Name }) -join "; "))
} else {
  Write-Wn "  WARN  no VB-Cable / ASIO / X18 sound device seen. Install VB-Cable; this launcher does not install drivers."
}

$names = New-Object string[] ($Channels + 1)
for ($i = 1; $i -le $Channels; $i++) { $names[$i] = ("CH{0:D2}" -f $i) }

$oscOk = $false
if ($OscHost -and $OscHost -ne "auto") {
  $st = Send-OscQuery $OscHost $OscPort "/status"
  if (Bytes-Has $st "/status") {
    $oscOk = $true
    Write-Host "  OSC handshake: ${OscHost}:${OscPort} /status OK"
    for ($i = 1; $i -le $Channels; $i++) {
      $ii = "{0:D2}" -f $i
      $path = "/ch/$ii/config/name"
      $rx = Send-OscQuery $OscHost $OscPort $path
      if (Bytes-Has $rx $path) {
        $txt = [System.Text.Encoding]::ASCII.GetString($rx).Replace("`0", "")
        $idx = $txt.IndexOf(",s")
        if ($idx -ge 0) { $txt = $txt.Substring($idx + 2) }
        $names[$i] = Sanitize $txt ("CH$ii")
      }
    }
    for ($i = 1; $i -le $Channels; $i++) {
      $base = $names[$i]; $nm = $base; $n = 2
      $j = 1
      while ($j -lt $i) {
        if ($names[$j] -eq $nm) { $nm = "${base}_$n"; $n++; $j = 1; continue }
        $j++
      }
      $names[$i] = $nm
    }
  } else {
    Write-Wn "  WARN  no /status from ${OscHost}:${OscPort} — using CH01…CH18 names"
  }
} else {
  Write-Wn "  WARN  XAIR_OSC_HOST not set — using CH01…CH18 names"
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "templates") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root "reaper-osc") | Out-Null

$oscFile = Join-Path $Root "reaper-osc\xair_network_bridge.ReaperOSC"
@"
# X-Air Network Bridge — REAPER OSC surface
# Load in REAPER: Preferences → Control/OSC/Web → Add → Load this file.
# This launcher never writes %APPDATA%\REAPER or reaper.ini.
# Local listen = $ReaperListen (REAPER receives). Destination = 127.0.0.1:$BridgeListen.

DEVICE_NAME "X-Air Network Bridge"
DEVICE_IN 0.0.0.0
DEVICE_PORT_IN $ReaperListen
DEVICE_OUT 127.0.0.1
DEVICE_PORT_OUT $BridgeListen

TRACK_COUNT $Channels
SEND_COUNT 4
RECEIVE_COUNT 4
FX_COUNT 8
FX_PARAM_COUNT 16
MARKER_COUNT 0
REGION_COUNT 0

REAPER_TRACK_FOLLOWS REAPER
DEVICE_TRACK_FOLLOWS DEVICE
DEVICE_TRACK_BANK_FOLLOWS DEVICE
DEVICE_FX_FOLLOWS DEVICE

TRACK_NAME s/track/@/name
TRACK_MUTE t/track/@/mute
TRACK_SOLO t/track/@/solo
TRACK_REC_ARM t/track/@/recarm
TRACK_VOLUME n/track/@/volume
TRACK_PAN n/track/@/pan
TRACK_SEND_VOLUME n/track/@/send/@/volume
TRACK_SEND_PAN n/track/@/send/@/pan

MASTER_VOLUME n/master/volume
MASTER_PAN n/master/pan
MASTER_MUTE t/master/mute

DEVICE_TRACK_COUNT n/device/track/count
REWIND t/rewind
PLAY t/play
STOP t/stop
RECORD t/record
"@ | Set-Content -Encoding ascii -LiteralPath $oscFile
Write-Host "  wrote $oscFile"

$rpp = Join-Path $Root "templates\xair_network_bridge.RPP"
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine('<REAPER_PROJECT 0.1 "7.0" 0')
[void]$sb.AppendLine('  RIPPLE 0')
[void]$sb.AppendLine('  GROUPOVERRIDE 0 0 0')
[void]$sb.AppendLine('  AUTOXFADE 1')
[void]$sb.AppendLine('  ENVATTACH 1')
[void]$sb.AppendLine('  MIXERUIFLAGS 11 48')
[void]$sb.AppendLine('  CURSOR 0')
[void]$sb.AppendLine('  ZOOM 100 0 0')
[void]$sb.AppendLine('  VZOOMEX 6 0')
[void]$sb.AppendLine('  RECMODE 1')
[void]$sb.AppendLine('  LOOP 0')
[void]$sb.AppendLine('  RECORD_PATH "Media" ""')
[void]$sb.AppendLine("  SAMPLERATE $Sr 0 0")
[void]$sb.AppendLine('  TEMPO 120 4 4')
[void]$sb.AppendLine('  PLAYRATE 1 0 0.25 4')
[void]$sb.AppendLine('  MASTERAUTOMODE 0')
[void]$sb.AppendLine('  MASTERHWOUT 0 0 1 0 0 0 0 0')
[void]$sb.AppendLine('  MASTER_NCH 2')
[void]$sb.AppendLine('  MASTER_VOLUME 1 0 -1 -1 1')
[void]$sb.AppendLine('  MASTER_FX 1')
[void]$sb.AppendLine('  <NOTES 0 2')
[void]$sb.AppendLine('    |X-Air Network Bridge — REAPER template')
[void]$sb.AppendLine('    |Host OS: Windows  audio: WASAPI/ASIO')
[void]$sb.AppendLine("    |Virtual cable (VB-Cable): DAW input = bridge output. 18-ch needs an aggregator.")
[void]$sb.AppendLine("    |Linux JACK names: ${ClientName}:NN_*_out / *_in")
[void]$sb.AppendLine('    |USB Return: last track, post-fader sends from 1–18, HWOUT 1–2 (stereo)')
[void]$sb.AppendLine('    |Plugins: always on track/bus OUTPUT feeding USB Return. Never input FX.')
[void]$sb.AppendLine("    |OSC: load ./reaper-osc/xair_network_bridge.ReaperOSC  listen $ReaperListen → dest 127.0.0.1:$BridgeListen")
[void]$sb.AppendLine('  >')
for ($i = 1; $i -le $Channels; $i++) {
  $ii = "{0:D2}" -f $i
  $guid = "{" + [guid]::NewGuid().ToString() + "}"
  $stem = "${ii}_$($names[$i])"
  $recin = $i - 1
  [void]$sb.AppendLine("  <TRACK $guid")
  [void]$sb.AppendLine("    NAME `"$stem`"")
  [void]$sb.AppendLine('    PEAKCOL 16576')
  [void]$sb.AppendLine('    BEAT -1')
  [void]$sb.AppendLine('    AUTOMODE 0')
  [void]$sb.AppendLine('    VOLPAN 1 0 -1 -1 1')
  [void]$sb.AppendLine('    MUTESOLO 0 0 0')
  [void]$sb.AppendLine('    IPHASE 0')
  [void]$sb.AppendLine('    PLAYOFFS 0 1')
  [void]$sb.AppendLine('    ISBUS 0 0')
  [void]$sb.AppendLine('    BUSCOMP 0 0 0 0 0')
  [void]$sb.AppendLine('    NCHAN 2')
  [void]$sb.AppendLine('    FX 1')
  [void]$sb.AppendLine('    TRACKHEIGHT 0 0 0 0 0 0')
  [void]$sb.AppendLine('    INQ 0 0 0 0.5 100 0 0 100')
  [void]$sb.AppendLine('    NRECARM 0')
  [void]$sb.AppendLine("    REC 0 $recin 1 1 0 0 0 0")
  [void]$sb.AppendLine('    VU 2')
  [void]$sb.AppendLine("    TRACKID $guid")
  [void]$sb.AppendLine('    PERF 0')
  [void]$sb.AppendLine('    MIDIOUT -1')
  [void]$sb.AppendLine('    MAINSEND 1 0')
  [void]$sb.AppendLine("    AUXSEND $Channels 0 0 0 0 0 0 0 0 -1:U 0 -1 ''")
  [void]$sb.AppendLine('  >')
}
$rguid = "{" + [guid]::NewGuid().ToString() + "}"
[void]$sb.AppendLine("  <TRACK $rguid")
[void]$sb.AppendLine('    NAME "USB_Return"')
[void]$sb.AppendLine('    PEAKCOL 255')
[void]$sb.AppendLine('    BEAT -1')
[void]$sb.AppendLine('    AUTOMODE 0')
[void]$sb.AppendLine('    VOLPAN 1 0 -1 -1 1')
[void]$sb.AppendLine('    MUTESOLO 0 0 0')
[void]$sb.AppendLine('    IPHASE 0')
[void]$sb.AppendLine('    PLAYOFFS 0 1')
[void]$sb.AppendLine('    ISBUS 0 0')
[void]$sb.AppendLine('    BUSCOMP 0 0 0 0 0')
[void]$sb.AppendLine('    NCHAN 2')
[void]$sb.AppendLine('    FX 1')
[void]$sb.AppendLine('    TRACKHEIGHT 0 0 0 0 0 0')
[void]$sb.AppendLine('    INQ 0 0 0 0.5 100 0 0 100')
[void]$sb.AppendLine('    NRECARM 0')
[void]$sb.AppendLine('    REC 0 -1 0 0 0 0 0 0')
[void]$sb.AppendLine('    VU 2')
[void]$sb.AppendLine("    TRACKID $rguid")
[void]$sb.AppendLine('    PERF 0')
[void]$sb.AppendLine('    MIDIOUT -1')
[void]$sb.AppendLine('    MAINSEND 0 0')
[void]$sb.AppendLine('    HWOUT 0 0 1 0 0 0 0 0')
[void]$sb.AppendLine('  >')
[void]$sb.AppendLine('>')
[System.IO.File]::WriteAllText($rpp, $sb.ToString())
Write-Host "  wrote $rpp  (18 stems + USB Return, post-fader sends)"

$udpListen = $null
try { $udpListen = Get-NetUDPEndpoint -LocalPort $ReaperListen -ErrorAction SilentlyContinue } catch { $udpListen = $null }
if ($udpListen) {
  Write-Host "  OSC listen :$ReaperListen already bound (REAPER Control/OSC likely enabled)"
} else {
  Write-Wn "  NOTE  Enable OSC in REAPER Preferences (this script does not edit reaper.ini):"
  Write-Host "         Load $oscFile"
  Write-Host "         Local listen $ReaperListen  →  Destination 127.0.0.1:$BridgeListen"
}

$running = Get-Process -Name reaper -ErrorAction SilentlyContinue
if ($running) {
  Write-Host "  REAPER already running — open $rpp from File → Open (session not restarted)"
} else {
  Start-Process -FilePath $reaper -ArgumentList "`"$rpp`""
  Write-Host "  launched REAPER with the template"
}

Write-Host ""
if ($portsOk -and $oscOk) {
  Write-Ok "REAPER READY — X-Air Network Bridge configurado correctamente."
  exit 0
}
Write-Wn "REAPER template written, but not fully READY (WASAPI/ASIO=$portsOk OSC=$oscOk)."
Write-Host "Start xair_network_bridge_client, install VB-Cable if needed, set XAIR_OSC_HOST, then re-run."
exit 1
