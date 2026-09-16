# DAW integration (REAPER, Bitwig, Ardour)

Generic notes. No user home paths, no studio IPs.

Audio device on Linux: JACK/PipeWire client **`xair_net_bridge`**.

- DAW **inputs** = `xair_net_bridge:NN_*_out` (dry stems from the X18).
- DAW **outputs** = `xair_net_bridge:NN_*_in` (duplex graph).
- Sample rate **48000**. Block size should match the JACK period (64 or 128 is typical).
- Do not select HDMI as the monitor device.

Desk IP is yours. Documentation placeholder:

```text
XAIR_DESK_IP="192.168.1.100"
```

Use that value in `XAIR_OSC_HOST` / `XAIR_OSC_IP`.

## REAPER

Launchers (write files under this repo only; they do not edit `reaper.ini`):

```bash
bash launchers/xair_network_bridge/xair_network_bridge_reaper_setup.sh
```

Windows: `launchers/xair_network_bridge/xair_network_bridge_reaper_setup.ps1`  
macOS: `launchers/xair_network_bridge/xair_network_bridge_reaper_setup.command`

Open `templates/xair_network_bridge.RPP`. Load `./reaper-osc/xair_network_bridge.ReaperOSC`.

OSC ports:

| Direction | Port |
| --- | --- |
| REAPER local listen | 8000 |
| Bridge listen (REAPER destination) | 9001 |
| X18 OSC | 10024 |

Then: `bash scripts/launch_osc.sh`

## Bitwig

```bash
bash launchers/xair_network_bridge/xair_network_bridge_bitwig_setup.sh
```

Settings → Audio → JACK. Optional script: `./bitwig-osc/xair_network_bridge.control.js`.  
Helper project: `templates/xair_network_bridge.bwproject`.

## Hybrid FX (optional)

Plugins on the **output** chain (or a post-fader send), never on hardware input FX.  
Stereo return: `XAIR_RETURN_AUDIO=true` → X18 USB playback 1–2. See the public `README.md`.

## Ardour

Same JACK device `xair_net_bridge`. Create 18 tracks from `*_out`. No extra template is shipped.
