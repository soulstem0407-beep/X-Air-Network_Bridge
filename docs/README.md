# X-Air Network Bridge

Puente de audio en red: **X18 (USB, clase audio) → captura PortAudio → UDP → cliente → salida a dispositivo virtual** visto por el DAW.

## Arquitectura (resumen)

- **Captura:** `sounddevice` (PortAudio). La X18 no usa `libusb` de aplicación para PCM multicanal estable; el perfil USB Audio Class se expone al SO y PortAudio es la vía documentada.
- **Protocolo:** datagramas UDP con cabecera `XBRI`, secuencia para reordenar jitter, carga PCM16/PCM24 o FLAC (vía `soundfile`/libsndfile). Opcional: XOR FEC (`XAIR_FEC`) y transporte Opus (`XAIR_OPUS`).
- **Cliente:** buffer de jitter por `seq`, cola hacia `OutputStream`.
- **Virtual:** no se crea desde Python un dispositivo con nombre comercial “X-AIR Network Bridge”. Usa **VB-Cable** (Windows), **BlackHole** (macOS) o **snd-aloop** (Linux); el DAW mostrará el nombre del driver instalado.

## Limitaciones reales

- Latencia 8–12 ms depende del tamponado (`XAIR_SAMPLES_PER_PACKET`, `XAIR_JITTER_PACKETS`, drivers).
- **18 canales:** en Linux, `xair_network_bridge_client` publica un cliente JACK/PipeWire full-duplex (`xair_net_bridge:01_Kick_in` / `01_Kick_out`, …). En Windows/macOS los cables virtuales suelen ser estéreo y hace falta un agregador extra.
- **FLAC:** más CPU y algo más de latencia que PCM en LAN; útil si el ancho de banda es justo.

## Requisitos

- Python 3.10+
- PortAudio (instalado junto a `sounddevice` en muchas plataformas; en Linux suelen faltar `libportaudio2` / `portaudio19-dev` según distro).
- libsndfile para FLAC (`soundfile`).
- `libopus` only if `XAIR_OPUS=true` (package `libopus0` on Debian/Ubuntu).

## Instalación

```bash
cd /path/to/X-Air-Network-Bridge
bash scripts/install.sh
cp .env.example .env
# Edit .env: set XAIR_OSC_HOST to YOUR mixer IP (placeholder: 192.168.1.100)
```

Paquetes Linux (sin cambiar el runtime):

```bash
PYTHONPATH=. python -m src.cli package-deb   # o bash scripts/package_deb.sh
PYTHONPATH=. python -m src.cli package-tar   # o bash scripts/package_tar.sh
PYTHONPATH=. python -m src.cli build-release # o bash scripts/build_release.sh
# artefactos en dist/; el .deb instala /usr/bin/xair-network-bridge y unidades systemd de usuario
```

## Arranque

Máquina con X18 (servidor):

```bash
bash scripts/xair_network_bridge_server.sh
```

Máquina con dispositivo virtual (cliente):

```bash
bash scripts/xair_network_bridge_client.sh
```

Wrapper CLI (`/usr/bin/xair-network-bridge` in the `.deb`):

```bash
bash scripts/xair-network-bridge --help
```

Read-only doctor (does not change JACK, PipeWire, quantum, or `.env`):

```bash
./xair_network_bridge_doctor.sh
```

DAW project templates (repo files only):

```bash
bash launchers/xair_network_bridge/xair_network_bridge_reaper_setup.sh
bash launchers/xair_network_bridge/xair_network_bridge_bitwig_setup.sh
```

O con el venv activo:

```bash
source .venv/bin/activate
PYTHONPATH=. python -m src.cli status
PYTHONPATH=. python -m src.cli xair_network_bridge_server
# En otra máquina/host:
PYTHONPATH=. python -m src.cli xair_network_bridge_client
```

### Comandos CLI

- `xair_network_bridge_server` — captura y envía.
- `xair_network_bridge_client` — escucha y reproduce.
- `set-port <puerto>` — guarda en `.xair_runtime.json`.
- `set-buffer <paquetes>` — suelo del jitter del cliente (`XAIR_JITTER_PACKETS`; con adaptive on el target puede subir hasta `XAIR_JITTER_MAX`).
- `status` — muestra configuración combinada (.env + runtime).
- `sync-status`, `sync-detail` — resumen/tabla del snapshot OSC+audio (cliente con `XAIR_SYNC_ON_CLIENT`). Incluye `integration_health` / `last_integration_ts`.
- `dashboard` — TUI de medidores/salud (o `--http` en 127.0.0.1:8765). Requiere el mismo snapshot. Grabación: tecla `r` / botones HTTP.
- `record-start`, `record-stop`, `record-status` — sidecar WAV por canal (`XAIR_RECORD` / `XAIR_RECORD_PATH`).
- `test` — suite offline (transporte, jitter, FEC, Opus, write-back OSC). Sin mesa. Escribe `.xair_test.json` (`last_test_run`, `test_health`).
- `install-service`, `remove-service` — unidad systemd de usuario (`xair_network_bridge_client` / `xair_network_bridge_server`). Reload = SIGHUP; parada = SIGTERM. No cambia jitter/FEC/Opus.
- `package-deb`, `package-tar` — artefactos en `dist/` (.deb Ubuntu/Debian y tarball genérico). Snapshot `package_version` / `package_build_ts`.
- `build-release` — pipeline de release: `.deb` + tarball + unidades systemd, flags `-O2`/`-s`, stamp de versión, `SOURCE_DATE_EPOCH`. Snapshot `release_version` / `release_build_ts`. No cambia transporte ni OSC.
- `xair-network-status` — OSC: `/status` con la mesa (`XAIR_OSC_*`).
- `xair-network-get-fader`, `xair-network-set-fader`, `xair-network-get-gain`, `xair-network-set-gain` — OSC (ver `docs/OSC.md`).
- `xair-get-mute`, `xair-set-mute`, `xair-get-pan`, `xair-set-pan` — OSC canal.
- `xair-get-send`, `xair-set-send`, `xair-get-bus`, `xair-set-bus`, `xair-get-lr`, `xair-set-lr` — OSC send/bus/LR.
- `xair-get-eq`, `xair-set-eq`, `xair-get-gate`, `xair-set-gate`, `xair-get-dyn`, `xair-set-dyn` — OSC EQ / gate / compresor.
- `xair-get-fx-send`, `xair-set-fx-send`, `xair-get-fx-return`, `xair-set-fx-return`, `xair-get-fx`, `xair-set-fx` — OSC FX.
- `scene-dump PATH`, `scene-recall PATH` — guarda/restaura mix (nombres, faders, gain, pan, mute, sends, buses, LR) como JSON. Solo OSC; ver `docs/OSC.md`.
- `name-ports` — preview de nombres OSC → puertos JACK (`xair_net_bridge:01_Kick_in` / `01_Kick_out`, …). No mueve audio.
- `start-reaper-sync` — OSC bidireccional X18 ↔ REAPER (proceso aparte del audio UDP; ver `docs/OSC.md`).
- `osc-launcher` — un comando: activa OSC, comprueba X18/REAPER, arranca el cliente si falta y lanza `start-reaper-sync`. Script: `bash scripts/launch_osc.sh`. Guía: [`docs/OSC_LAUNCHER.md`](OSC_LAUNCHER.md).
- `discover` — escucha mDNS/SSDP/`/xinfo` y beacons del puente (`XAIR_DISCOVERY`). Lista nombre, modelo, IP, firmware.

## ALSA loopback (Linux)

Opcional si no usas los puertos JACK: `sudo modprobe snd-aloop` (requiere permisos). Comprueba `/proc/asound/cards`. Luego en `.env` algo que coincida con PortAudio, por ejemplo `XAIR_OUTPUT_DEVICE=Loopback`.

## Puertos JACK/PipeWire (Linux)

Con `xair_network_bridge_client`, **sin** `XAIR_OUTPUT_DEVICE`, el receptor abre un cliente JACK `xair_net_bridge` (PipeWire-JACK) **full-duplex**: un puerto de **entrada** (`_in`, capture) y uno de **salida** (`_out`, playback) por canal, en el mismo orden 1…N. Los stems salen de `/ch/XX/config/name` (`01_Kick_in` / `01_Kick_out`, …). REAPER ve 18 inputs y 18 outputs.

- Requiere el paquete `JACK-Client` (lo instala `scripts/install.sh`) y PipeWire/JACK a la misma tasa que `XAIR_SAMPLE_RATE` (48 kHz).
- OSC se lee **al arrancar**, no en el callback de audio. Si la mesa no responde, los puertos quedan `01_CH01_in` / `01_CH01_out`.
- `XAIR_VIRTUAL_PORT_NAMES=false` vuelve al sink Pulse `module-null-sink` + PortAudio (puertos AUX/FL, sin nombres de canal). En modo JACK el dúplex no se puede apagar con `.env`.
- `python -m src.cli name-ports` muestra el mapa in/out sin abrir audio.
- No auto-conecta a los altavoces (`node.autoconnect=false`). El patchbay forzado es `System input → xair_net_bridge → REAPER → System output`.
- El tamaño de buffer JACK (**Frames/Period** en QjackCtl) lo eliges tú (64–2048). Si PipeWire lo devuelve a 1024, el cliente lo vuelve a fijar (`clock.force-quantum` + `clock.quantum`) y un watchdog lo restaura si el engine salta otra vez. No reconecta el grafo en ese momento.
- La interfaz física no puede ser HDMI: el cliente pone el sink/source PipeWire por defecto en analog (onboard analog-stereo) y, si QjackCtl tiene `Interface=hw:HDMI`, lo cambia al `hw:` analog. El patchbay usa puertos analog (alias HDMI en `system:*` se descartan).

## Problemas comunes JACK/PipeWire

Si Frames/Period vuelve a **1024**, QjackCtl elige **`hw:HDMI`**, hay drift/jitter/XRUNs, o REAPER no ve 18+18:

- Guía: [`docs/README_JACK_PIPEWIRE.md`](README_JACK_PIPEWIRE.md)
- Script (no borra el programa, no toca UDP/jitter/FEC/Opus/server):

```bash
bash tools/fix_jack_pipewire.sh
```

Cierra QjackCtl, para el cliente, lanza el script o relanza `xair_network_bridge_client`, abre QjackCtl (Interface analog, Frames/Period 64 o 128) y comprueba `python3 -m src.cli sync-status` / `name-ports`.

## Validación de la X18 en Ubuntu Studio

Para comprobar lista PortAudio, nombre de la mesa, backend (ALSA / JACK / PipeWire vía `pactl`), soporte de **18 entradas a 48 kHz** y una **apertura de stream de prueba (~200 ms)** sin grabar audio:

```bash
source .venv/bin/activate
PYTHONPATH=. python -m src.usb_capture.validate
```

Código de salida: `0` todo correcto; `1` fallo (mesa no detectada, menos de 18 canales, errores al abrir stream o flags `input_overflow` / `input_underflow` en PortAudio); `2` no Linux.

Requisitos típicos Ubuntu Studio: usuarios en grupos de audio, PipeWire activo, firmware USB estable. Si `pactl` no existe, la detección PipeWire/Pulse se basa sólo en el host API de PortAudio (suele aparecer como ALSA aunque PW esté debajo).

## Solución de problemas

- **No hay 18 entradas:** la X18 debe estar en modo USB multicanal esperado por el SO; verifica en el mezclador del sistema.
- **Cortes / ruido:** sube `XAIR_JITTER_PACKETS` o baja `XAIR_SAMPLES_PER_PACKET` para MTU; revisa Wi‑Fi (preferible cable). En Wi‑Fi sucio, `XAIR_FEC=true` en ambos extremos puede recuperar una pérdida por grupo. `XAIR_OPUS=true` reduce ancho de banda (WAN); hace falta `libopus`.
- **`ModuleNotFoundError`:** ejecuta siempre con el `PYTHONPATH=.` del proyecto o desde `scripts/`.
- **`externally-managed-environment` (pip):** usa el `venv` que crea `install.sh`.

## Comparativa: X AIR App vs X-Air Network Bridge

La app oficial X AIR (X18/XR18) y este puente no se sustituyen: una controla la mesa; el otro lleva **audio multicanal** e integración DAW por red.

| Característica | X AIR App | X-Air Network Bridge |
| --- | --- | --- |
| Control de la consola (faders, EQ, comp, FX) | Sí. UI táctil/escritorio pensada para mezclar en vivo. | Parcial. OSC CLI, `start-reaper-sync` y write-back hacia la mesa; no es un mezclador visual. |
| Transporte de audio por red (LAN/WiFi) | No. El USB de 18 canales queda en el PC enchufado a la mesa. La app no envía stems por IP. | Sí. `xair_network_bridge_server` captura USB y manda 18 ch por UDP (`XBRI`); `xair_network_bridge_client` los recibe (jitter, FEC/Opus opcionales). |
| Integración con DAWs (REAPER, Ardour, etc.) | No como dispositivo de 18 pistas. El DAW no ve la app como interfaz. | Sí. En Linux, cliente JACK/PipeWire `xair_net_bridge`; el DAW abre 18 entradas/salidas. |
| Exposición de 18 canales vía JACK/PipeWire | No. | Sí. Full-duplex `*_in` / `*_out`, stems OSC (`01_Kick_in` / `01_Kick_out`). |
| Sincronización OSC bidireccional (nombres, faders, mute, pan) | Control mesa → app. No sincroniza el DAW. | Sí. `osc-launcher` / `start-reaper-sync`: mesa ↔ REAPER (fader, mute, pan, nombres, sends, buses, LR). |
| Multitrack recorder (18 canales) | No (o solo lo que mezcles/grabes fuera). | Sí. Sidecar WAV por canal (`XAIR_RECORD`, `record-start`). |
| Mezcla híbrida (física + digital) | Mezcla en la X18; la app es el remoto. | USB en escenario + DAW en otra máquina; FOH en mesa y stems en REAPER a la vez. |
| Return USB hacia la consola | No vía app. | Opcional. `XAIR_RETURN_AUDIO` / `XAIR_RETURN_OSC`: 2 ch DAW → UDP → USB de la mesa. |
| Automatización vía CLI / scripts | No. | Sí. `python -m src.cli …`, escenas, systemd, `osc-launcher`, tests. |
| Reemplazo de Dante/AVB (audio-over-IP) | No. | Aproximación USB+Ethernet en LAN (no es Dante/AES67). Útil si no hay stage-box AoIP. |
| Compatibilidad con Linux | Limitada o nula (apps móviles/Windows/macOS). | Primaria. Ubuntu Studio, PipeWire/JACK, paquetes `.deb`. |
| Uso sin DAW | Sí. Mezclas y escuchas en la mesa/app. | El audio de red apunta a un dispositivo virtual/JACK; sin DAW pierdes el caso de uso principal. |
| Uso sin GUI | No. La app es la interfaz. | Sí. CLI, TUI/`dashboard`, unidades systemd. |
| Requisitos técnicos | Mesa en red, app oficial, poco setup. | Python 3.10+, PortAudio, PipeWire/JACK en el cliente, `.env`, LAN (cable preferible). Más piezas que pueden fallar (buffer 1024, HDMI, puertos OSC). |

### ¿Por qué existe X-Air Network Bridge?

La **X AIR App** es excelente para **control visual** de la X18/XR18: faders, EQ, dinámicas y FX en un panel que cualquier operador reconoce.

No ofrece **audio por red** (18 stems hacia otro PC), **integración DAW** (JACK/PipeWire), **recorder** multitrack ni **OSC bidireccional** con REAPER. El USB de clase audio sigue atado a una sola máquina.

El **X-Air Network Bridge** llena ese vacío para usuarios avanzados, **Linux**, broadcast, DAWs y workflows híbridos: la mesa puede quedarse en escenario y el DAW en otra sala, con 18 canales nombrados, grabación por pista y sync de control — sin sustituir la app para mezclar en vivo.

## Licencia

Código ejemplo de proyecto; revisa uso con tu equipo y red.
