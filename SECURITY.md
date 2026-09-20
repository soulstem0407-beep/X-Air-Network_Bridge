# Security policy and deployment model

## Supported use

X-Air Network Audio Bridge is designed for a trusted production LAN. XBRI audio, return audio, discovery, dashboard controls and OSC are not encrypted protocols. Do not expose their UDP/HTTP ports directly to the public Internet.

## Safe deployment checklist

1. Put the mixer, bridge host and DAW host on a dedicated VLAN or trusted wired LAN.
2. Set `XAIR_PEER_HOST` to the fixed IP of the expected sender. The receiver rejects XBRI audio from other source IPs.
3. Restrict UDP 50000/50001, discovery 50010 and OSC ports with the host firewall to the two bridge hosts and mixer.
4. Bind dashboards and control listeners to loopback unless remote access is required.
5. Use WireGuard or another authenticated VPN if traffic must cross an untrusted network.
6. Do not rely on source-IP filtering as cryptographic authentication; spoofing may still be possible on a hostile LAN.

## Protocol validation

The receiver rejects unknown XBRI versions/flags, unreasonable sample rates, channel/frame counts and inconsistent uncompressed payload sizes before decoding. These checks reduce accidental corruption and denial-of-service risk; they do not replace authentication.

## Reporting a vulnerability

Open a private security advisory with the repository owner when possible. Include the affected version, reproduction steps, impact and whether the issue is remotely reachable. Avoid publishing working exploits before a fix is available.
