# X-AIR Network Audio Bridge

Transport X-Air multitrack audio over Ethernet and make it available on another workstation without requiring a direct USB connection between the mixer and the DAW.

USB class-audio stays on the mixer PC (X18/XR18) → 18-channel `XBRI` UDP → JACK/PipeWire for the DAW. Optional OSC to the desk, sidecar WAV recorder, stereo USB return for hybrid FX.

**New to this?** Start here: [`INSTRUCCIONES_PARA_DUMMIES.md`](INSTRUCCIONES_PARA_DUMMIES.md) (plain-language steps).

**Fresh boot → audio + OSC demo** (community cheat sheet): [`docs/DEMO.md`](docs/DEMO.md).

Install, CLI, and JACK/PipeWire troubleshooting: [`docs/README.md`](docs/README.md). OSC: [`docs/OSC.md`](docs/OSC.md), [`docs/OSC_LAUNCHER.md`](docs/OSC_LAUNCHER.md). DAWs: [`docs/DAW.md`](docs/DAW.md).

CLI after install: `xair-network-bridge` (`/usr/bin/xair-network-bridge`). From the repo:

```bash
bash scripts/install.sh
cp .env.example .env
# Set YOUR mixer IP. Placeholder only:
# XAIR_OSC_HOST=192.168.1.100
bash scripts/xair-network-bridge --help
bash scripts/xair clean                 # hung PIDs, UDP ports, corrupt state
bash scripts/xair_network_bridge_server.sh   # USB mixer PC
bash scripts/xair_network_bridge_client.sh   # DAW PC
./xair_network_bridge_doctor.sh              # read-only check
```

DAW templates (no JACK/quantum/user-config changes):

```bash
bash launchers/xair_network_bridge/xair_network_bridge_reaper_setup.sh
bash launchers/xair_network_bridge/xair_network_bridge_bitwig_setup.sh
```

The official **X AIR app** is the visual remote for the mixer. This project is the **network multicore into the DAW**. They complement each other; they do not replace each other.

## X AIR App vs X-Air Network Audio Bridge

| Feature | X AIR App | X-Air Network Audio Bridge |
| --- | --- | --- |
| Console control (faders, EQ, dyn, FX) | Yes. Touch/desktop UI for live mix. | Partial. OSC CLI, `osc-launcher` / `start-reaper-sync`, write-back. Not a mixer GUI. |
| OSC control | Desk → app only. No DAW sync. | Yes. Desk ↔ DAW (fader, mute, pan, names, sends, buses, LR) and CLI one-shots. |
| Network audio (LAN/Wi‑Fi) | No. 18-ch USB stays on the PC plugged into the desk. | Yes. `start-server` captures USB; `start-client` receives 18 ch (`XBRI`; jitter, optional FEC/Opus). |
| DAW integration (REAPER, Bitwig, Ardour) | No. The app is not an 18-stem audio interface. | Yes. Linux JACK/PipeWire client `xair_net_bridge`: 18 in + 18 out. |
| JACK/PipeWire ports | No. | Yes. Duplex `*_in` / `*_out`, OSC stems (`01_Kick_in` / `01_Kick_out`). |
| 18-channel recorder | No (unless you record elsewhere). | Yes. Sidecar PCM24 WAV per channel (`record-start`). Independent of the DAW. |
| Hybrid FX (DAW plugins → USB return) | No plugin host, no third-party DSP return. Four onboard FX slots only. | Yes. Stereo `XAIR_RETURN_AUDIO` to USB 1–2 (Valhalla, Waves, FabFilter, SSL, Auto-Tune, …). |
| CLI / script automation | No. | Yes. `python -m src.cli`, scenes, systemd, shell macros. |
| Hybrid analog + digital mix | Mix lives on the X18; the app is the remote. | USB on stage + DAW in another room; FOH on the desk and stems in the DAW. |
| USB return to the console | Not via the app. | Optional. DAW → UDP → X18 USB (`XAIR_RETURN_AUDIO`), with a safety latch. |
| Dante/AVB stand-in | No. | USB + Ethernet on a LAN (not Dante/AES67). |
| Linux | Little or none (mobile / Windows / macOS). | Primary. Ubuntu Studio, PipeWire/JACK, `.deb`. |
| Use without a DAW | Yes. You mix on the desk/app. | Audio path is JACK/virtual; recorder and CLI still run headless. |
| Use without a GUI | No. | Yes. CLI, TUI `dashboard`, user systemd. |
| Technical cost | Desk on the LAN, official app, little setup. | Python 3.10+, PortAudio, PipeWire/JACK, `.env`, wired LAN preferred. |

## Advanced X-Air Network Bridge benefits

- **Audio-over-IP (LAN/Wi‑Fi)** — 18 channels, default **PCM24**, `XBRI` UDP. USB stays at the mixer; the DAW can be elsewhere.
- **Adaptive jitter and optional FEC/Opus** — Client jitter buffer + sequence reorder. `sync-status` reports local timing indicators; its `drift` value is **not** a shared-clock or PTP measurement. Clean wired Ethernet is recommended. Wi‑Fi is best-effort.
- **REAPER, Bitwig, Ardour** — Any JACK host. Same `xair_net_bridge` device.
- **18 inputs + 18 outputs via JACK/PipeWire** — Permanent full-duplex. Default patchbay is **manual** (qpwgraph/REAPER/Bitwig). Optional `XAIR_JACK_FORCE_GRAPH=true`: System → X-Air Network Bridge → REAPER → System (analog, never HDMI).
- **Hybrid analog + digital mix** — Preamps and faders on the X18; stems, buses, and plugins in the DAW.
- **Stems, buses, digital FX, USB return** — Named OSC stems into the DAW; stereo wet return to the desk as extra FX.
- **CLI and OSC automation** — Faders, mute, names, scenes, recorder, `osc-launcher`.
- **Practical X-Air LAN bridge** — Not Dante, AVB, AES67 or a replacement for their clocking, interoperability, redundancy or security. It is USB class-audio + unicast UDP for a controlled LAN.

## Hybrid FX (REAPER → X18)

REAPER (or Bitwig/Ardour) can run Valhalla, Waves, FabFilter, SSL, iZotope, Auto-Tune, etc., and send the **wet** signal back to the X18 over **USB return**, as if those plugins were extra console FX (not the four built-in X AIR engines).

**Flow:** X18 USB capture → UDP → JACK `xair_net_bridge:NN_*_out` → DAW tracks → plugins on the **output** chain → stereo bus → `XAIR_RETURN_AUDIO` → UDP 50001 → `start-server` USB playback → X18 Routing USB 1–2 into the live mix.

The X AIR app cannot do this: no plugin host, no 18-stem network feed, no third-party DSP back into the desk.

Network return is **stereo (2 ch)**, not 18 independent inserts. JACK `*_in` is duplex for the DAW graph, **not** the USB return. Keep `XAIR_RETURN_SAFETY=true`.

**Setup**

1. REAPER **inputs** = `xair_net_bridge:NN_*_out` (dry from the desk). Those ports are bridge **playback**, not a REAPER hardware output.
2. Process on the track/bus **output** (see below). Build a stereo FX or mix bus.
3. `XAIR_RETURN_AUDIO=true`. `XAIR_RETURN_INPUT_DEVICE` = stereo capture of that bus. On the mixer PC, `XAIR_RETURN_OUTPUT_DEVICE` = **X18 USB playback** (never HDMI).
4. X18: Setup → Routing → **USB**. Assign **USB 1–2** to FX returns/channels. POST-fader sends into the DAW (see next section). Wired LAN; JACK period = DAW block (64/128).

**Real-world examples**

- Valhalla VintageVerb as a plate on USB 1–2 (room/vocal, without using an onboard FX slot).
- Waves R-Vox / CLA on the vocal stem, returned to FOH.
- FabFilter Pro-Q 3 for feedback notches or surgical EQ on a bus.
- SSL Channel Strip as a digital insert on a group.
- Auto-Tune live only if USB+LAN latency is acceptable for the singer.

### Where do the plugins go?

**Always on the output path. Never on the input.**

| Wrong | Right |
| --- | --- |
| Hardware **input FX** / record-arm inserts on the JACK capture | Track (or bus) **FX chain / output** that feeds the USB return |
| Processing “on the input” before the dry hits the fader | Dry in from `*_out` → channel fader → **then** inserts/sends → stereo out to return capture |

Input FX would treat the DAW like a preamp insert: wet no longer follows POST-fader, you print processed audio into the recorder, and the USB return no longer behaves like a console FX unit. Hybrid FX are an **aux/insert after the dry**, same as a hardware rack on an output send.

## PRE vs POST-fader for hybrid FX

**Hybrid FX sends are always POST-fader.**

The DAW is a rack on an aux. FOH dry is the channel fader. If the USB feed into REAPER is POST, pulling the fader (or mute) drops dry **and** the plugin feed, so wet/dry stay in proportion.

**PRE-fader** taps before the fader. Mute the vocal and REAPER still returns full-level wet: FX louder than dry, unstable mix, verb in the gaps, loop risk if the return is in LR.

**PRE-fader is for IEM / cue mixes** — the musician still needs the send when FOH is down. Do not use PRE for the hybrid FX USB path.

**X18:** Sends → tap = **POST** (not PRE) on the bus/aux (or USB mix) that feeds the bridge. Not a pre-fader direct out unless you are splitting IEMs.

**DAW:** Same rule on sends inside REAPER/Bitwig: post-fader to the verb bus that hits `XAIR_RETURN_AUDIO`.

## Multitrack recorder (18 channels)

Sidecar on `start-client`: one PCM24 WAV per channel under `XAIR_RECORD_PATH` (default `recordings/`, session folders `YYYYMMDDTHHMMSSZ/`). The UDP thread only `copy` + `put_nowait`; the JACK callback never hits disk.

```bash
# Client already running
PYTHONPATH=. python3 -m src.cli record-start
PYTHONPATH=. python3 -m src.cli record-status
PYTHONPATH=. python3 -m src.cli record-stop
```

`XAIR_RECORD=true` starts a session when the client boots. Dashboard: key `r` (TUI) or HTTP record buttons. Status also shows in `sync-status` when sync is on.

**Why it exists:** backup independent of the DAW project; you still have 18 stems if REAPER crashes or was never armed. It is not a substitute for a DAW session if you need edits/plugins on disk — it is the safety net next to the hybrid mix.

## Why record all 18 channels?

Recording **Main L/R** is a stereo picture of the live mix. If the vocal was quiet, the kick was too loud, or the FX were too wet, that stereo file is already baked. You can master it, but you cannot un-mix it.

Recording **all 18 channels** is a multitrack session: one dry file per input (kick, snare, bass, vocal, …). After the show you still have every source, at the same sample rate as the desk (`PCM24`, 48 kHz). That is the difference between a souvenir mix and a studio project.

**What you can do later**

- Remix the show with a cool head: new fader balance, new buses, new FX.
- Fix mistakes: a missed mute, a shout into a hot mic, a guitar that sat too loud for one song.
- Improve vocals and drums with premium plugins (Valhalla, Waves, FabFilter, SSL, Auto-Tune, iZotope) that the X18 cannot run on its own.
- Print stems, acapellas, and instrumentals for video, radio, or remixes.
- Master from a real mix, not from a compromised live two-track.

**Hybrid offline mix (for beginners)**

Live, the X18 is FOH: preamps, faders, and the room. The bridge records the 18 USB stems **before** you have to live with that mix forever. Offline, you open those WAVs in REAPER/Bitwig/Ardour and mix as if the gig had been a tracking session. Physical desk on the night; digital mix the next morning. That is hybrid: live analog path + later DAW mix from the same 18 channels.

Simple examples:

- Vocal too quiet in the hall → raise the vocal stem 3 dB; you do not need a time machine.
- Kick burying the mix → lower the kick stem; the rest of the kit stays put.
- FX too loud → the stems are dry; you add less verb in the DAW instead of living with the live send.

**Virtual soundcheck**

Play the 18 recorded tracks back through the same routing (USB return / DAW → X18, or DAW monitors) with the band off stage. You walk the room, tune EQ and FX, and save scenes **as if the musicians were still there**. That is virtual soundcheck: yesterday’s show becomes today’s line check, without asking the band to replay the set.

Recording 18 channels turns any gig into a **studio session you already tracked**. The X AIR app can mix the night; it cannot give you that tape. The bridge recorder can.

## Advanced automation

OSC one-shots talk to the X18 (`XAIR_OSC_HOST` / `XAIR_OSC_IP`). Combine with `osc-launcher` for REAPER follow. Mute CLI: **1 = muted**, **0 = open**.

```bash
PYTHONPATH=. python3 -m src.cli xair-set-fader 1 --value 0.75
PYTHONPATH=. python3 -m src.cli xair-set-mute  1 --value 1
PYTHONPATH=. python3 -m src.cli xair-set-pan   1 --value 0.5
PYTHONPATH=. python3 -m src.cli xair-set-send  1 1 --value 0.3
PYTHONPATH=. python3 -m src.cli name-ports      # OSC names → 01_Kick_in / _out
PYTHONPATH=. python3 -m src.cli scene-dump scenes/show.json
PYTHONPATH=. python3 -m src.cli scene-recall scenes/show.json
PYTHONPATH=. python3 -m src.cli osc-launcher
PYTHONPATH=. python3 -m src.cli record-start
```

Channel names: `get_channel_name` / `set_channel_name` in OSC (see [`docs/OSC.md`](docs/OSC.md)); `name-ports` maps them to JACK. Scenes dump/recall names, faders, gain, pan, mute, sends, buses, LR (not meters).

**Shows, scenes, and macros:** `scene-recall` at changeover; a shell script can chain mute, fader, `record-start`, and `osc-launcher --check` as a scene-change macro. systemd (`install-service --role client`) starts the audio path at login. Desk EQ/dyn/FX: `xair-set-eq`, `xair-set-dyn`, `xair-set-fx` (control thread, not JACK). This is automation the official app does not expose.

## Bitwig integration

Bitwig on Linux uses JACK/PipeWire the same way REAPER does.

1. PipeWire JACK running; `start-client` already created `xair_net_bridge`.
2. Bitwig → Settings → Audio → **JACK** (PipeWire). Sample rate **48000**, block size = QjackCtl Frames/Period (64 or 128). The PipeWire stereo “audio engine” auto-links to the default device and will not record 18 stems.
3. **Inputs:** 18 sources `xair_net_bridge:NN_*_out` (dry from the X18). Patch in qpwgraph. Create 18 tracks or a multi-in chain.
4. **Outputs:** do not use HDMI. Hybrid FX: stereo bus → device that `XAIR_RETURN_INPUT_DEVICE` captures. Main monitors: analog System playback. Leave qpwgraph **Patchbay → Activated** OFF while you recable.
5. **Plugins:** on the **track/device output** chain (or a send to an FX track whose output is the return bus). Never on hardware input FX. POST-fader sends to that FX track.
6. `XAIR_RETURN_AUDIO=true` and X18 USB 1–2 as for REAPER.

QjackCtl: Interface analog, not `hw:HDMI`. Guide: [`docs/README_JACK_PIPEWIRE.md`](docs/README_JACK_PIPEWIRE.md).

## Why X-Air Network Bridge exists

The **X AIR app** is excellent for **visual control** of the X18/XR18: faders, EQ, dynamics, and FX on a panel every operator already knows.

It does **not** offer network audio, DAW integration (REAPER/Bitwig/Ardour), an 18-channel recorder, bidirectional OSC with the DAW, or hybrid FX (plugins returning over USB). Class-audio USB stays tied to one machine.

The **X-Air Network Bridge** fills that gap for advanced users, **Linux**, broadcast, DAWs, and hybrid live/studio workflows: desk on stage, DAW in another room, named stems, optional recorder and USB return — without replacing the app for mixing FOH on the X18.

## Scope, alternatives, and security

NetJACK is an established alternative when the requirement is JACK audio transport between computers. This project is justified by its X18-specific workflow: USB capture at the desk, OSC control and naming, scenes, recorder, dashboard, packaging, and guarded stereo return. Evaluate both on the same hardware; this project does not claim lower latency or greater reliability without measurements.

XBRI and OSC are intended for a **trusted, isolated LAN**. They are not encrypted. The client filters XBRI audio by `XAIR_PEER_HOST`, but that is not cryptographic authentication. Use host firewalls and a VPN such as WireGuard across untrusted networks. See [`SECURITY.md`](SECURITY.md).

## Support the Project

This project is **free and open**. Anyone can use the bridge — no account, no license fee, no paywall.

If the tool helps your shows or studio and you want to support development, you are welcome to donate via PayPal. It is optional. The bridge stays free either way.

[💙 Support via PayPal](https://www.paypal.com/donate?email=delvallet651@gmail.com)

PayPal: [delvallet651@gmail.com](mailto:delvallet651@gmail.com)

Donations help keep the project alive: documentation, new features, and compatibility with more systems. Thank you if you chip in — and thank you for using it if you do not.

## License

This project is licensed under the **GNU General Public License v3.0** ([`LICENSE`](LICENSE)).

GPLv3 is there to protect the author and the community:

- It keeps others from taking the code, claiming it as their own, or shipping it as closed software.
- Anyone who distributes a modified version must keep that version open under GPLv3 (copyleft).
- Distributed modified versions must satisfy GPLv3 source obligations. Private modifications that are not conveyed generally do not have to be published.
- Voluntary donations (see Support above) are compatible with GPLv3. Charging for copies or support is allowed; locking the source is not.
- Third parties may use and study the bridge, including commercially; when they convey covered binaries or modified versions, GPLv3 obligations apply.

You may run, share, and modify the bridge under GPLv3. The full legal text is in [`LICENSE`](LICENSE).
