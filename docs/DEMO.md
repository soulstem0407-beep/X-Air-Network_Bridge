# Fresh boot demo: audio + OSC

This is a controlled-LAN demonstration, not a production latency benchmark.

## Mixer/USB host

1. Connect the X18/XR18 by USB and wired Ethernet.
2. Run `bash scripts/install.sh`, copy `.env.example` to `.env`, and set the DAW computer's fixed IP in `XAIR_PEER_HOST`.
3. Confirm `XAIR_SAMPLE_RATE=48000`, `XAIR_CHANNELS=18`, and start:

   ```bash
   bash scripts/xair_network_bridge_server.sh
   ```

## DAW host

1. Install the project and set `XAIR_PEER_HOST` to the mixer/USB host's fixed IP. This is also the accepted XBRI source.
2. Start PipeWire/JACK and then:

   ```bash
   bash scripts/xair_network_bridge_client.sh
   ```

3. Open qpwgraph or the DAW and verify the 18 `xair_net_bridge` ports.
4. Run `PYTHONPATH=. python3 -m src.cli sync-status` and listen for clean audio before enabling optional FEC, Opus, recorder or return audio.

## OSC

Set `XAIR_OSC_HOST` to the mixer's IP, then run:

```bash
PYTHONPATH=. python3 -m src.cli xair-network-status
PYTHONPATH=. python3 -m src.cli osc-launcher --check
```

Keep XBRI/OSC on a trusted LAN and apply the firewall guidance in `SECURITY.md`.
