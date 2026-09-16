# Official Bitwig setup launcher (Windows).
# Linux: xair_network_bridge_bitwig_setup.sh   macOS: xair_network_bridge_bitwig_setup.command
#
# Writes project-local template + OSC controller. Does NOT change WASAPI/ASIO
# device selection, sample rate, buffers, HDMI, Bitwig user dir, or .env.
# GET-only OSC to the X18.
#
# Usage: powershell -ExecutionPolicy Bypass -File launchers/xair_network_bridge/xair_network_bridge_bitwig_setup.ps1

$ErrorActionPreference = "Stop"
$Channels = 18
$OscPortDefault = 10024
$DawListen = 8000
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

function Xml-Esc([string]$s) {
  return ($s -replace '&', '&amp;' -replace '<', '&lt;' -replace '>', '&gt;' -replace '"', '&quot;')
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

function Find-Bitwig {
  $cmd = Get-Command bitwig-studio -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $p = Join-Path ${env:ProgramFiles} "Bitwig Studio\Bitwig Studio.exe"
  if (Test-Path $p) { return $p }
  $found = Get-ChildItem (Join-Path ${env:ProgramFiles} "Bitwig Studio") -Filter "Bitwig Studio.exe" -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($found) { return $found.FullName }
  return $null
}

$envmap = Get-DotEnv (Join-Path $Root ".env")
$ClientName = Cfg $envmap "XAIR_JACK_CLIENT_NAME" $ClientName
$OscHost = Cfg $envmap "XAIR_OSC_HOST" ""
if (-not $OscHost) { $OscHost = Cfg $envmap "XAIR_OSC_IP" "" }
$OscPort = [int](Cfg $envmap "XAIR_OSC_PORT" "$OscPortDefault")
$DawListen = [int](Cfg $envmap "XAIR_REAPER_PORT" "$DawListen")
$BridgeListen = [int](Cfg $envmap "XAIR_REAPER_LISTEN_PORT" "$BridgeListen")
$Sr = [int](Cfg $envmap "XAIR_SAMPLE_RATE" "48000")

Write-Host "X-Air Network Bridge — Bitwig setup (project files only)" -ForegroundColor Cyan
Write-Host "OS: Windows  audio backend: WASAPI/ASIO"
Write-Host "Does not change WASAPI/ASIO device, buffers, HDMI, or ~/Bitwig Studio"

$bitwig = Find-Bitwig
if (-not $bitwig) { Die "Bitwig is not installed (Program Files\Bitwig Studio)" }
Write-Host "  Bitwig: $bitwig"

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
New-Item -ItemType Directory -Force -Path (Join-Path $Root "bitwig-osc") | Out-Null

$js = Join-Path $Root "bitwig-osc\xair_network_bridge.control.js"
@"
// X-Air Network Bridge — Bitwig controller script
// Same OSC surface as REAPER so start-reaper-sync / osc-launcher work.
// Load: Bitwig Settings → Controllers → Add → Hardware → Script
//       point at this file. User config is left untouched.
// Windows audio: WASAPI/ASIO (VB-Cable). Plugins on OUTPUT / USB Return only.

loadAPI(17);

host.defineController(
  "X-Air Network Bridge",
  "X-Air Network Bridge OSC",
  "1.0.0",
  "c0ffee00-18a1-4b17-9e00-000000000018",
  "X-Air"
);
host.defineMidiPorts(0, 0);

var TRACKS = $Channels;
var OSC_OUT_HOST = "127.0.0.1";
var OSC_OUT_PORT = $BridgeListen;
var OSC_IN_PORT = $DawListen;

function init() {
  println("X-Air Network Bridge OSC init  in=" + OSC_IN_PORT + "  out=" + OSC_OUT_HOST + ":" + OSC_OUT_PORT);
  var osc = host.getOscModule();
  var space = osc.createAddressSpace();
  space.setShouldConsumeEvents(false);
  var bank = host.createMainTrackBank(TRACKS, 1, 0);
  var i;
  for (i = 0; i < TRACKS; i++) {
    (function (idx) {
      var t = bank.getItemAt(idx);
      t.name().markInterested();
      t.volume().markInterested();
      t.pan().markInterested();
      t.mute().markInterested();
    })(i);
  }
  space.registerMethod("/track/{n}/volume", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) { bank.getItemAt(n - 1).volume().set(args[0], 1); }
  });
  space.registerMethod("/track/{n}/pan", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) { bank.getItemAt(n - 1).pan().set(args[0], 1); }
  });
  space.registerMethod("/track/{n}/mute", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) { bank.getItemAt(n - 1).mute().set(!!args[0]); }
  });
  space.registerMethod("/track/{n}/name", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) { bank.getItemAt(n - 1).name().set(String(args[0])); }
  });
  try { osc.createUdpServer(OSC_IN_PORT, space); } catch (e) { println("OSC bind " + e); }
  try { osc.connectToUdpServer(OSC_OUT_HOST, OSC_OUT_PORT, space); } catch (e) { println("OSC TX " + e); }
  println("USB Return: post-fader send; plugins on OUTPUT only.");
}
function flush() {}
function exit() { println("X-Air Network Bridge OSC exit"); }
"@ | Set-Content -Encoding utf8 -LiteralPath $js
Write-Host "  wrote $js"

$tmp = Join-Path $env:TEMP ("xair-bw-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$tracksJson = New-Object System.Collections.Generic.List[string]
for ($i = 1; $i -le $Channels; $i++) {
  $ii = "{0:D2}" -f $i
  $stem = "${ii}_$($names[$i])"
  $comma = if ($i -eq $Channels) { "" } else { "," }
  $tracksJson.Add(('    {"index": ' + $i + ', "name": "' + $stem + '", "input": "' + $ClientName + ':' + $stem + '_out", "output": "' + $ClientName + ':' + $stem + '_in", "send": "USB_Return", "send_mode": "post-fader"}' + $comma))
}
$routing = @"
{
  "name": "X-Air Network Bridge",
  "jack_client": "$ClientName",
  "sample_rate": $Sr,
  "audio_backend": "WASAPI/ASIO",
  "tracks": [
$($tracksJson -join "`n")
  ],
  "usb_return": {
    "name": "USB_Return",
    "channels": 2,
    "plugins": "output chain only",
    "note": "Capture this stereo bus with XAIR_RETURN_INPUT_DEVICE"
  }
}
"@
Set-Content -Encoding utf8 -LiteralPath (Join-Path $tmp "routing.json") -Value $routing

$xml = New-Object System.Text.StringBuilder
[void]$xml.AppendLine('<?xml version="1.0" encoding="UTF-8"?>')
[void]$xml.AppendLine('<Project version="1.0">')
[void]$xml.AppendLine('  <Application name="X-Air Network Bridge" version="1.0"/>')
[void]$xml.AppendLine('  <Transport><Tempo value="120" unit="bpm"/></Transport>')
[void]$xml.AppendLine('  <Structure>')
for ($i = 1; $i -le $Channels; $i++) {
  $ii = "{0:D2}" -f $i
  $stem = Xml-Esc ("${ii}_$($names[$i])")
  [void]$xml.AppendLine("    <Track name=`"$stem`" contentType=`"audio`" loaded=`"true`">")
  [void]$xml.AppendLine('      <Channel audioChannels="1" role="regular"/>')
  [void]$xml.AppendLine('      <Sends><Send destination="USB_Return" type="post" volume="0"/></Sends>')
  [void]$xml.AppendLine('    </Track>')
}
[void]$xml.AppendLine('    <Track name="USB_Return" contentType="audio" loaded="true">')
[void]$xml.AppendLine('      <Channel audioChannels="2" role="effect"/>')
[void]$xml.AppendLine('    </Track>')
[void]$xml.AppendLine('  </Structure>')
[void]$xml.AppendLine('</Project>')
[System.IO.File]::WriteAllText((Join-Path $tmp "project.xml"), $xml.ToString())

@"
X-Air Network Bridge — Bitwig template
Windows audio: WASAPI/ASIO (VB-Cable). Linux: JACK $ClientName. macOS: CoreAudio BlackHole.
USB Return: stereo FX track, POST-fader sends, plugins on OUTPUT only.
OSC script: bitwig-osc/xair_network_bridge.control.js
Listen $DawListen → dest 127.0.0.1:$BridgeListen
"@ | Set-Content -Encoding utf8 -LiteralPath (Join-Path $tmp "XAIR_NETWORK_BRIDGE.txt")

$bw = Join-Path $Root "templates\xair_network_bridge.bwproject"
if (Test-Path $bw) { Remove-Item -LiteralPath $bw -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($tmp, $bw)
Remove-Item -Recurse -Force $tmp
Write-Host "  wrote $bw  (18 stems + USB Return routing map)"

$running = Get-Process | Where-Object { $_.Name -match 'Bitwig' }
if ($running) {
  Write-Host "  Bitwig already running — File → Open $bw (session not restarted)"
} else {
  Start-Process -FilePath $bitwig -ArgumentList "`"$bw`""
  Write-Host "  launched Bitwig with the template"
}

Write-Host "  Bitwig audio: Settings → Audio → WASAPI or ASIO. Not written by this script."
Write-Host "  Controller: Settings → Controllers → add $js"

Write-Host ""
if ($portsOk -and $oscOk) {
  Write-Ok "BITWIG READY — X-Air Network Bridge configurado correctamente."
  exit 0
}
Write-Wn "Bitwig template written, but not fully READY (WASAPI/ASIO=$portsOk OSC=$oscOk)."
Write-Host "Start xair_network_bridge_client, install VB-Cable if needed, set XAIR_OSC_HOST, then re-run."
exit 1
