# Instructions for beginners — X-Air Network Bridge

This guide uses **plain language**. You do not need to be a programmer.

Replace every example IP with **your** mixer address. The example used everywhere is:

```text
XAIR_DESK_IP="192.168.1.100"
```

That is a **placeholder**, not a real studio. Look up the X18/XR18 address in the mixer’s Setup → Network screen (or in the official X AIR app).

This project never needs your home folder, your login name, or your computer’s hostname.

---

## 1. What is this?

You have a Behringer **X18 / XR18** mixer.

The official **X AIR app** is a remote control: faders, EQ, FX on a screen.

**X-Air Network Bridge** is different. It is a **network snake into a DAW** (REAPER, Bitwig, Ardour):

1. A computer next to the mixer captures the 18 USB channels.
2. Those channels travel over your LAN as UDP packets (`XBRI`).
3. A computer with the DAW receives them and shows 18 inputs (on Linux: JACK client `xair_net_bridge`).
4. Optionally, OSC keeps mixer faders in sync with REAPER.

The app still mixes FOH. This project sends **stems** to the DAW.

---

## 2. What you need

- Behringer X18 or XR18, USB connected to the **server** PC.
- A wired LAN is best (Wi‑Fi is possible, less reliable).
- Python 3.10 or newer.
- Linux is the main platform (PipeWire/JACK). Windows/macOS need a virtual cable (VB-Cable / BlackHole).
- REAPER or Bitwig if you want a DAW.

Typical layout:

| Machine | Role | What runs |
| --- | --- | --- |
| PC next to the mixer | **Server** | `xair_network_bridge_server` |
| PC with the DAW | **Client** | `xair_network_bridge_client` |

You can use **one PC** for both. Then the “other computer” IP is `127.0.0.1`.

---

## 3. Install (Linux)

Open a terminal **in the project folder** (the folder that contains `README.md`).

```bash
bash scripts/install.sh
cp .env.example .env
```

Open `.env` in any text editor.

Set the mixer IP (placeholder shown):

```text
XAIR_OSC_HOST=192.168.1.100
XAIR_OSC_IP=192.168.1.100
```

If the DAW is **another** computer, set that computer’s LAN IP:

```text
XAIR_PEER_HOST=192.168.1.101
```

If server and client are the **same** computer:

```text
XAIR_PEER_HOST=127.0.0.1
```

Do not copy anyone else’s `.env`. That file stays on your machine (it is listed in `.gitignore`).

---

## 4. Run it

### Server (USB mixer PC)

```bash
bash scripts/xair_network_bridge_server.sh
```

Leave this window open.

### Client (DAW PC)

```bash
bash scripts/xair_network_bridge_client.sh
```

Leave this window open too.

Check:

```bash
bash scripts/xair-network-bridge status
./xair_network_bridge_doctor.sh
```

The doctor script is **read-only**. It does not change JACK, PipeWire, or your settings.

After install, the command name is `xair-network-bridge` (`/usr/bin/xair-network-bridge` in the `.deb`).

---

## 5. How network audio works (simple)

```text
X18 USB  →  server PC  →  LAN (UDP)  →  client PC  →  JACK  →  DAW
 18 ch       capture       XBRI           receive      18 in+18 out
```

- **Server** = “take USB from the mixer and send packets.”
- **Client** = “receive packets and make a virtual sound card.”
- On Linux the virtual card is named **`xair_net_bridge`**.
- DAW **inputs** = ports ending in `_out` (dry from the mixer).
- DAW **outputs** = ports ending in `_in` (full duplex graph). HDMI speakers are never used on purpose.

This is **not** Dante or AES67. It is USB + Ethernet on your LAN.

---

## 6. Use with REAPER (Linux)

1. Start the **client** first (step 4).
2. Create the template (does **not** change REAPER’s own settings):

```bash
bash launchers/xair_network_bridge/xair_network_bridge_reaper_setup.sh
```

3. In REAPER: File → Open → `templates/xair_network_bridge.RPP`
4. Audio device: **JACK** (PipeWire), 48000 Hz.
5. You should see 18 tracks `01_CH01` … `18_CH18`.
6. Load OSC: Preferences → Control/OSC/Web → Enable OSC → load
   `./reaper-osc/xair_network_bridge.ReaperOSC`
   - Local listen port: **8000**
   - Destination: `127.0.0.1` port **9001**

Windows: run `xair_network_bridge_reaper_setup.ps1`  
macOS: double-click `xair_network_bridge_reaper_setup.command`

---

## 7. Use with Bitwig (Linux)

1. Start the **client** first.
2. Create the template:

```bash
bash launchers/xair_network_bridge/xair_network_bridge_bitwig_setup.sh
```

3. Bitwig → Settings → Audio → **JACK**, 48000 Hz.
4. Inputs: `xair_net_bridge:NN_*_out`
5. Optional controller script: `./bitwig-osc/xair_network_bridge.control.js`
6. Project helper: `templates/xair_network_bridge.bwproject`

Plugins for “hybrid FX” go on the **output** of a track or bus, never on the hardware input. USB return to the mixer is stereo (channels 1–2 on the X18 USB playback).

---

## 8. Turn on OSC (mixer ↔ DAW)

OSC is **control**, not audio. It moves faders/names. Audio still uses the UDP path above.

1. Mixer and DAW PC on the same LAN.
2. In `.env`:

```text
XAIR_OSC_ENABLED=true
XAIR_OSC_HOST=192.168.1.100
XAIR_OSC_PORT=10024
XAIR_REAPER_HOST=127.0.0.1
XAIR_REAPER_PORT=8000
XAIR_REAPER_LISTEN_PORT=9001
```

3. Enable OSC inside REAPER (listen **8000**, destination **9001**).
4. Run:

```bash
bash scripts/launch_osc.sh
```

or:

```bash
PYTHONPATH=. python3 -m src.cli osc-launcher --host 192.168.1.100
```

You should see messages that X18, OSC, REAPER, and sync are OK.

Quick test (does not start audio):

```bash
PYTHONPATH=. python3 -m src.cli xair-network-status --host 192.168.1.100
PYTHONPATH=. python3 -m src.cli xair-network-get-fader 1 --host 192.168.1.100
```

---

## 9. If something fails

| Problem | What to try |
| --- | --- |
| No 18 channels | X18 USB mode, cables, `xair_network_bridge_server` running |
| No sound in the DAW | Client running? Device is `xair_net_bridge`, not HDMI |
| OSC timeout | Wrong mixer IP; use the placeholder pattern with **your** address |
| REAPER “address already in use” | Listen 8000, bridge listen 9001 — two different ports |
| Cuts / crackles | Prefer Ethernet; see `docs/README_JACK_PIPEWIRE.md` |

More detail: `docs/README.md`, `docs/OSC.md`, `docs/DAW.md`, `README.md`.

---

## 10. Support and license

The software is free (GPLv3). Donations are optional:

[💙 Support via PayPal](https://www.paypal.com/donate?email=delvallet651@gmail.com)

Full legal text: [`LICENSE`](LICENSE).
