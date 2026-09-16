# JACK / PipeWire — problemas comunes (cliente)

Guía para el **cliente** (`xair_network_bridge_client`) en Linux con PipeWire-JACK y QjackCtl.
No cambia UDP, jitter, FEC, Opus, recorder, OSC, el server ni los nombres de puerto.

La X18 por USB vive en **`xair_network_bridge_server`**. En la máquina del DAW no tiene que aparecer `hw:X18`; el cliente es `xair_net_bridge` y la interfaz física de monitors debe ser **analog** (onboard analog-stereo), nunca `hw:HDMI`.

Script de arreglo (no borra el programa):

```bash
bash tools/fix_jack_pipewire.sh
```

---

## Qué está pasando

PipeWire (WirePlumber / *policy-node*) elige dos cosas por política de sesión, no por lo que pongas en QjackCtl:

1. **`clock.quantum`** — el periodo del grafo. El valor de fábrica suele ser **1024**. Si nadie fija `clock.force-quantum`, QjackCtl escribe 64 o 128 y ~100 ms después el reloj vuelve a 1024.
2. **Primera tarjeta ALSA** — en `/proc/asound/cards` HDMI suele ser la tarjeta 0 (`hw:HDMI`). El sink por defecto y el campo **Interface** de QjackCtl caen ahí. Los puertos JACK `system:playback_*` **no llevan “hdmi” en el nombre**, así que parecen “System” aunque el PCM sea HDMI.

`xair_network_bridge_client` ya intenta corregirlo (pin del quantum, sink analog, reescritura de `Interface=hw:HDMI`). Si QjackCtl sigue abierto con el engine arrancado sobre HDMI, o PipeWire limpia el metadata, el síntoma vuelve. Esta guía y el script lo dejan explícito.

---

## Síntomas

| Síntoma | Causa habitual |
| --- | --- |
| Frames/Period **vuelve a 1024** solo | `clock.quantum=1024` y `clock.force-quantum` vacío o 0 |
| Interface = **`hw:HDMI`** | HDMI es la primera tarjeta ALSA / sink por defecto |
| QjackCtl **no muestra la X18** | En el **cliente** no hay X18 USB. En el **server**, PortAudio/QjackCtl eligió HDMI |
| Drift / jitter / cortes | Periodo 1024 + HDMI (reloj del display) o REAPER a otra tasa |
| XRUNs en QjackCtl | Periodo demasiado bajo **o** salto 64→1024 a mitad de ciclo |
| REAPER sin 18 in + 18 out | Cliente no dúplex, o audio device ≠ JACK (PipeWire) |

Valores sanos con el cliente en marcha (`XAIR_SYNC_ON_CLIENT=true`):

```text
jitter_ema=0.0
drift=0
loss_ppm=0.0
```

Cero absoluto en jitter no siempre es realista en Wi‑Fi; en LAN cableada y 48 kHz / 64–128 frames debe quedar cerca de eso. `loss_ppm` alto o drift que crece = periodo inestable o Interface HDMI.

---

## Solución paso a paso

Hazlo **en este orden**. No borres el repo ni el `.venv`.

### 1. Cerrar QjackCtl

Quita el engine (**Stop**) y cierra la ventana. Si QjackCtl sigue abierto, vuelve a leer `Interface=hw:HDMI` y reaplica 1024.

### 2. Parar el cliente

En la terminal del cliente: **Ctrl+C**.

Si quedó huérfano:

```bash
pkill -f 'src.cli xair_network_bridge_client' || true
# si usas la unidad de usuario:
# systemctl --user stop xair-network-bridge-client.service
```

No mates `xair_network_bridge_server` ni `pipewire`.

### 3. Relanzar el cliente

Desde la raíz del repo:

```bash
bash scripts/xair_network_bridge_client.sh
```

O:

```bash
source .venv/bin/activate
PYTHONPATH=. python3 -m src.cli xair_network_bridge_client
```

El cliente fija analog (no HDMI) y pinea el Frames/Period que vea en JACK (si no es 1024).

### 4. Abrir QjackCtl de nuevo

Arranca QjackCtl **después** del cliente. Setup → Parameters:

- **Interface** = analog (`hw:Generic`, `hw:PCH`, onboard analog, *nunca* `hw:HDMI` / NVIDIA HDMI)
- **Frames/Period** = **64** o **128** (48 kHz)
- **Sample Rate** = 48000 (`XAIR_SAMPLE_RATE`)

Start si hace falta. El grafo forzado es:

`System input → xair_net_bridge → REAPER → System output` (analog, no HDMI).

### 5. Comprobar que no vuelve a 1024

Espera ~2 s y mira Frames/Period otra vez. Debe seguir en 64 o 128.

```bash
pw-metadata -n settings 0
# clock.force-quantum y clock.quantum = 64 o 128, no 1024
```

Si salta a 1024: `bash tools/fix_jack_pipewire.sh --quantum 128` y repite desde el paso 1.

---

## Verificar sincronización (drift / jitter)

Con el cliente vivo y `XAIR_SYNC_ON_CLIENT=true` en `.env`:

```bash
PYTHONPATH=. python3 -m src.cli sync-status
```

Busca en la línea RX:

- `jitter_ema=…` → objetivo **0.0** ms (o muy bajo)
- drift global → **0** (`global_drift_ns` / drift)
- `loss_ppm=…` → **0.0**

Detalle por canal: `python3 -m src.cli sync-detail`. Medidores: `python3 -m src.cli dashboard`.

Si `sync-status` dice que SyncManager no está activo: activa `XAIR_SYNC_ON_CLIENT=true` y relanza el cliente.

---

## Verificar puertos dúplex

```bash
PYTHONPATH=. python3 -m src.cli name-ports
```

Debe listar el cliente `xair_net_bridge` y **18 `_in` + 18 `_out`** (`01_Kick_in` / `01_Kick_out`, o `01_CH01_*` si la mesa no nombra el canal). No abre audio.

En QjackCtl / `jack_lsp`: mismos 18+18. Si solo ves `*_out`, el proceso viejo no es el dúplex: mata el cliente y relanza.

---

## Sincronizar REAPER con el bridge

1. **Audio device** de REAPER = **JACK** (backend PipeWire-JACK), no ALSA/HDMI, no Pulse.
2. Sample rate **48000**, block size = Frames/Period de QjackCtl (64 o 128).
3. El cliente JACK se llama **`xair_net_bridge`**.
4. REAPER debe ver **18 inputs + 18 outputs**.
5. Cadena: capturas de sistema → `xair_net_bridge:*_in` → `xair_net_bridge:*_out` → entradas de REAPER → salidas de REAPER → analog (no HDMI).
6. No elijas `hw:HDMI` en QjackCtl ni en REAPER.

OSC faders/EQ (opcional, otro proceso, no es el audio UDP):

```bash
PYTHONPATH=. python3 -m src.cli start-reaper-sync
```

---

## Evitar `hw:HDMI` de forma permanente

- Deja que `xair_network_bridge_client` (o el script) ponga el sink/source PipeWire en **analog-stereo**.
- QjackCtl: `~/.config/rncbc.org/QjackCtl.conf` → `Interface=` analog. El cliente y el script reescriben `Interface=hw:HDMI`; **cierra QjackCtl** para que lo relea.
- No pongas `XAIR_OUTPUT_DEVICE` a nada con HDMI.
- En el **server**, la X18 es el dispositivo físico USB; HDMI está prohibido como fallback PortAudio.
- No hace falta apagar el perfil HDMI de la GPU (el script no lo hace).

Comprobar:

```bash
grep -E '^(Interface|InDevice|OutDevice)=' ~/.config/rncbc.org/QjackCtl.conf
pactl info | grep -E 'Default (Sink|Source)'
cat /proc/asound/cards
```

---

## Evitar `quantum=1024` de forma permanente

- Elige 64 o 128 en QjackCtl **con el cliente ya en marcha**.
- `xair_network_bridge_client` escribe `clock.force-quantum` + `clock.quantum` y un watchdog restaura si PipeWire vuelve a 1024.
- El script hace lo mismo a mano:

```bash
pw-metadata -n settings 0 clock.force-quantum 128
pw-metadata -n settings 0 clock.quantum 128
pw-metadata -n settings 0 clock.min-quantum 32
pw-metadata -n settings 0 clock.max-quantum 8192
```

`clock.min-quantum` / `max-quantum` se dejan anchos (32–8192) para que 64–2048 sigan siendo válidos. No pongas `node.lock-quantum = false` en `PIPEWIRE_PROPS`: eso es lo que dispara el snap a 1024.

El metadata de sesión se pierde al reiniciar PipeWire; vuelve a lanzar el cliente o el script.

---

## Cómo reportar problemas

No adjuntos del programa ni del `.venv`. Incluye:

1. Distro y `pw-metadata -n settings 0` (quantum / force-quantum).
2. `/proc/asound/cards` y `pactl info` (Default Sink/Source).
3. Líneas `Interface=` de QjackCtl.
4. `PYTHONPATH=. python3 -m src.cli name-ports`
5. `PYTHONPATH=. python3 -m src.cli sync-status` (jitter_ema, drift, loss_ppm).
6. Si REAPER ve 18 in + 18 out y el device es JACK (PipeWire).

No hace falta (y no se debe) borrar el proyecto para “resetear” JACK.
