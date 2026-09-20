# Changelog

## 0.1.1 — 2026-09-20

### Fixed

- Decouple the audio callback block (`XAIR_AUDIO_BLOCKSIZE`, default 256) from XBRI UDP packetization (`XAIR_SAMPLES_PER_PACKET`, default 21).
- Preserve Opus flags and exact variable payload lengths when XOR FEC reconstructs a lost packet.
- Reject incompatible FEC group metadata instead of silently merging it.
- Validate XBRI version, flags, sample rate, channels, frame counts and media payload sizes.
- Filter incoming XBRI audio by the configured `XAIR_PEER_HOST` source address.
- Restore the missing fresh-boot demo referenced by the README.

### Security and documentation

- Add a trusted-LAN threat model and deployment checklist.
- Clarify that source-IP filtering is not cryptographic authentication.
- Describe NetJACK as an alternative and define the X18-specific scope.
- Replace unmeasured “drift=0” and Dante/AVB replacement claims with accurate limitations.
- Correct the GPLv3 summary for private versus distributed modifications.

### Tests

- Add regression coverage for combined Opus + FEC recovery with variable payload lengths.
- Add negative protocol tests for unknown versions, flags and malformed PCM payloads.
