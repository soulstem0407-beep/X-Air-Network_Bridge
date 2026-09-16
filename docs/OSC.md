# Control OSC (X AIR / X18)

Este módulo habla por **OSC/UDP con la consola** (misión paralela al puente **audio UDP** documentado aparte).

## Fuente protocolo

Las rutas y reglas siguen la documentación *X AIR Mixer Series Remote Control Protocol* de MUSIC Group (véase también *Air-Series OSC Documentation* / firmware ZIP `Parameters.txt`):

- OSC por **UDP** hacia la consola; puerto por defecto **10024**.
- Las respuestas vuelven al **puerto desde el que se solicitó**.
- **`get`** de un parámetro: mismo path sin argumentos; la consola responde con el path + valor («echo»).
- **Float** habitualmente entre **0.0 … 1.0** para faders/gains (mapeados a dB en la consola).
- **Suscripción de meters**: mensaje **`/meters`** con typo **`,si`**: cadena **`/meters/N`** entero auxiliar tal como en los ejemplos binarios del PDF (para **`/meters/1`** el enterito se puede tunear con **`XAIR_OSC_METER_SUB_ARG`** si no llega respuesta).

## Variables de entorno

| Variable | Descripción |
|----------|--------------|
| `XAIR_OSC_HOST` | Mixer IP. Placeholder in docs: `192.168.1.100` (your address, not a public default). |
| `XAIR_OSC_PORT` | UDP (normalmente **10024**) |
| `XAIR_OSC_TIMEOUT` | Espera solicitud/res en segundos |
| `XAIR_OSC_XREMOTE` | `true`/`1` si quieres enviar **`/xremote`** antes de cada comando CLI one-shot (keep-alive típico de apps remotas). |
| `XAIR_OSC_XREMOTE_INTERVAL_SEC` | Keep-alive de la sesión `/xremote` de `start-reaper-sync` (por defecto 8 s; la mesa caduca a ~10 s). |
| `XAIR_OSC_METER_SUB_ARG` | Segundo argumento entero junto al string **`"/meters/1"`** (por defecto 9 hasta alinear exactamente con tu firmware). |
| `XAIR_SCENE_CHANNELS` | Canales a volcar/recordar (si falta, `XAIR_CHANNELS`, máx. 18). |
| `XAIR_SCENE_SENDS` / `XAIR_SCENE_BUSES` | Cuántos sends y buses incluir (por defecto 4). |
| `XAIR_EQ` / `XAIR_DYN` / `XAIR_FX` | `true` = control OSC de EQ, gate/compresor o FX en `xair_network_bridge_client` (hilo de control). |

## Rutas OSC usadas en código

Los canales se numeran **`01…18`** en el path OSC.

| Uso | Ruta OSC |
|-----|-----------|
| Fader canal | **`/ch/XX/mix/fader`** |
| Canal on/off (CLI mute invertido) | **`/ch/XX/mix/on`** |
| Pan canal | **`/ch/XX/mix/pan`** |
| Send canal | **`/ch/XX/mix/YY`** |
| Fader bus | **`/bus/XX/mix/fader`** |
| Fader master LR | **`/lr/mix/fader`** |
| Gain preamplificador entrada | **`/headamp/XX/gain`** |
| Etiqueta de canal | **`/ch/XX/config/name`** |
| EQ on | **`/ch/XX/eq/on`** |
| EQ banda 1–4 (type, gain, freq, Q) | **`/ch/XX/eq/N/{type,g,f,q}`** |
| HPF | **`/ch/XX/preamp/hpf`** |
| Gate | **`/ch/XX/gate/{on,thr,range,attack,hold,release}`** |
| Compresor | **`/ch/XX/dyn/{on,thr,ratio,knee,mgain,attack,hold,release,mix}`** |
| Send a FX 1–4 | **`/ch/XX/mix/0N`** (buses mix 01–04) |
| FX return | **`/rtn/0N/mix/{fader,on,pan}`** |
| FX tipo / params | **`/fx/N`**, **`/fx/N/par/NN`** |
| Medidores agregados (documentación serie) | Suscripción **`/meters`** con string **`"/meters/1"`**; blob de respuesta en **`/meters/1`**: primer **int32** BE = número de niveles **int16 BE** siguientes (resolución dB típica 1/256). |

Implementación práctica `get_meter(ch)`: usa el elemento **índice `ch − 1`** del vector tras el int32 inicial. Según modelo/firmware puede no coincidir con el mismo slot visual del mezclador respecto al manual detallado (orden del batch `/meters/1` está en la lista *ALL CHANNELS* del PDF).

Diagnóstico de presencia/consola sin audio:

- **`ping()`** envía **`/status`** y espera **`/status`** con argumentos (p. ej. `active`, `standby`).

## API Python (`src/xair_control/osc_bridge.py`)

Clase **`XAirOSCBridge`**: método **`from_env(...)`**, context manager que cierra el socket UDP.

Funciones objeto instancia solicitadas:

- `get_fader(ch)` · `set_fader(ch, value)`
- `get_gain(ch)` · `set_gain(ch, value)`
- `get_mute(ch)` · `set_mute(ch, state)` — **`/ch/XX/mix/on`** (1 = abierto). El CLI `xair-*-mute` invierte a 1 = muteado.
- `get_pan(ch)` · `set_pan(ch, value)`
- `get_send(ch, send_idx)` · `set_send(ch, send_idx, value)`
- `get_bus_fader(bus)` · `set_bus_fader(bus, value)`
- `get_lr_fader()` · `set_lr_fader(value)`
- `get_meter(ch)` — nivel int16 del blob ``/meters/1`` (caché corta; un subscribe rellena todos los canales)
- `get_channel_name(ch)` · `set_channel_name(ch, name)` — también alimentan los puertos JACK de `xair_network_bridge_client` (`python -m src.cli name-ports`).
- `get_eq_on` / `set_eq_on`, `get_eq_band` / `set_eq_band`, `get_hpf` / `set_hpf`
- `get_gate` / `set_gate`, `get_dyn` / `set_dyn`
- `get_fx_send` / `set_fx_send`, `get_fx_return` / `set_fx_return`, `get_fx_type` / `set_fx_type`, `get_fx_par` / `set_fx_par`
- `ping()`

Ejemplo rápido:

```python
from src.xair_control.osc_bridge import XAirOSCBridge

with XAirOSCBridge.from_env() as xr:
    print(xr.ping())
    print(xr.get_fader(1))
```

## CLI (`python -m src.cli`)

Comandos (opciones `--host` / `--port` anulan temporalmente `.env`):

```bash
PYTHONPATH=. python -m src.cli xair-network-status
PYTHONPATH=. python -m src.cli xair-network-get-fader  1
PYTHONPATH=. python -m src.cli xair-network-set-fader  1 --value 0.75
PYTHONPATH=. python -m src.cli xair-network-get-gain   1
PYTHONPATH=. python -m src.cli xair-network-set-gain   1 --value 0.5
PYTHONPATH=. python -m src.cli xair-get-mute   1
PYTHONPATH=. python -m src.cli xair-set-mute   1 --value 1
PYTHONPATH=. python -m src.cli xair-get-pan    1
PYTHONPATH=. python -m src.cli xair-set-pan    1 --value 0.5
PYTHONPATH=. python -m src.cli xair-get-send   1 1
PYTHONPATH=. python -m src.cli xair-set-send   1 1 --value 0.3
PYTHONPATH=. python -m src.cli xair-get-bus    1
PYTHONPATH=. python -m src.cli xair-set-bus    1 --value 0.8
PYTHONPATH=. python -m src.cli xair-get-lr
PYTHONPATH=. python -m src.cli xair-set-lr     --value 0.9
PYTHONPATH=. python -m src.cli xair-get-eq     1
PYTHONPATH=. python -m src.cli xair-set-eq     1 --on 1
PYTHONPATH=. python -m src.cli xair-set-eq     1 --band 2 --leaf g --value 0.6
PYTHONPATH=. python -m src.cli xair-get-gate   1
PYTHONPATH=. python -m src.cli xair-set-gate   1 --leaf thr --value 0.4
PYTHONPATH=. python -m src.cli xair-get-dyn    1
PYTHONPATH=. python -m src.cli xair-set-dyn    1 --leaf ratio --value 0.5
PYTHONPATH=. python -m src.cli xair-get-fx-send 1 1
PYTHONPATH=. python -m src.cli xair-set-fx-send 1 1 --value 0.3
PYTHONPATH=. python -m src.cli xair-get-fx-return 1
PYTHONPATH=. python -m src.cli xair-set-fx     1 --par 1 --value 0.2
PYTHONPATH=. python -m src.cli scene-dump      scenes/sala.json
PYTHONPATH=. python -m src.cli scene-recall    scenes/sala.json
PYTHONPATH=. python -m src.cli scene-recall    scenes/sala.json --dry-run
```

`--host` / `--port` anulan `XAIR_OSC_*`. Canal y bus son argumentos posicionales (`CH` 1–18, `SEND`/`BUS` 1–16). Mute en CLI: **1 = muteado**, **0 = abierto** (la mesa usa `/mix/on` al revés).

### Escenas JSON (`scene-dump` / `scene-recall`)

Solo control OSC (no USB/UDP). El JSON cubre nombres, faders, HA gain, pan, `/mix/on`, sends, faders de bus y LR. No incluye medidores ni EQ/dyn/FX.

Variables (`.env`): `XAIR_SCENE_CHANNELS` (si falta, `XAIR_CHANNELS`), `XAIR_SCENE_SENDS`, `XAIR_SCENE_BUSES` (por defecto 4). En dump, `--channels` / `--sends` / `--buses` anulan el alcance.

El documento usa `kind: "xair-scene"`, `schema: 1`. La lista de canales está en **`channel`**; **`channels`** es el recuento. Mute nativo: `mix_on` (1/true = abierto) más `muted` derivado. Recall prefiere `mix_on` y, si falta, invierte `muted`. Un timeout en un GET no aborta el dump: el campo se omite y queda en `errors`. Recall escribe `/mix/on` primero (canales muteados siguen callados) y por defecto continúa ante errores OSC (`--strict` aborta). `--dry-run` no abre la mesa.

### REAPER (`start-reaper-sync`)

Proceso **opcional** y **separado** de `xair_network_bridge_server` / `xair_network_bridge_client`. No entra en el camino USB/UDP de audio.

Launcher de un comando (comprueba X18 + REAPER, arranca el cliente si falta y entra en el sync):

```bash
bash scripts/launch_osc.sh
# o:
PYTHONPATH=. python -m src.cli osc-launcher
```

Guía: [`docs/OSC_LAUNCHER.md`](OSC_LAUNCHER.md).

A mano:

```bash
PYTHONPATH=. python -m src.cli start-reaper-sync
# o:
PYTHONPATH=. python -m src.osc.x18_reaper_sync
```

Variables (`.env`):

| Variable | Descripción |
|----------|-------------|
| `XAIR_REAPER_HOST` | IP de la máquina REAPER (destino OSC) |
| `XAIR_REAPER_PORT` | Puerto donde REAPER **escucha** OSC |
| `XAIR_REAPER_LISTEN_HOST` / `XAIR_REAPER_LISTEN_PORT` | Bind local para lo que REAPER **envía** a este puente |
| `XAIR_REAPER_USE_XREMOTE` | `true` (defecto): snapshot GET + `/xremote`. `false`: GET-poll periódico |
| `XAIR_OSC_XREMOTE_INTERVAL_SEC` | Keep-alive `/xremote` (por defecto 8) |
| `XAIR_REAPER_POLL_INTERVAL_SEC` | Solo si `USE_XREMOTE=false` |
| `XAIR_REAPER_ECHO_SUPPRESS_SEC` | Ventana anti-eco bidireccional |
| `XAIR_REAPER_CHANNELS` / `SENDS` / `BUSES` | Alcance del snapshot / de las suscripciones |

En REAPER: Local listen port = `XAIR_REAPER_PORT` (típico **8000**). Destination IP = este PC y Destination port = `XAIR_REAPER_LISTEN_PORT` (**9001**). Los dos puertos no pueden ser el mismo: REAPER ya ocupa el listen.

Sincroniza: fader, mute, pan, nombre, sends, buses, LR. El mute de REAPER (1 = muteado) se traduce a `/ch/XX/mix/on` de la mesa (1 = abierto). Un `EchoGuard` ignora ecos y valores iguales.

La mesa **no** vuelca el mix al suscribirse: el puente hace un GET inicial y luego vive de los pushes `/xremote` (mismo socket que GET/SET; las respuestas de transacción no se reenvían a REAPER). EQ/dyn/FX se ignoran en este proceso (van por `XAIR_EQ` / `XAIR_DYN` / `XAIR_FX` en el cliente).

### EQ / dynamics / FX (`XAIR_EQ` / `XAIR_DYN` / `XAIR_FX`)

Opcional, junto a `xair_network_bridge_client`. **No** modifica jitter, FEC, Opus, discovery ni el return-path DAW.

- **Lectura/escritura** de EQ de canal (on, HPF, bandas type/gain/freq/Q), gate, compresor, sends a FX 1–4 y returns (`/rtn/0N`), tipo y parámetros `/fx/N`.
- **Escritura en vivo** (`DspController`): cola + hilo `xair-dsp`. SET bajo `SyncManager.osc_io_lock`. El callback `/xremote` **solo actualiza memoria** (timestamps / `last_change`); nunca hace SET.
- CLI one-shot (`xair-get-eq`, `xair-set-dyn`, …) abre el puente en el proceso CLI, igual que `xair-network-set-fader`.
- Snapshot `report.dsp`: `eq_enabled` / `dyn_enabled` / `fx_enabled`, health `off|ok|warn|fail` (verde/amarillo/rojo), `last_*_write_ns`, `last_change`. Cada canal lleva `last_eq_write_ns` / `last_dyn_write_ns` / `last_fx_write_ns`.

### Return path (`XAIR_RETURN_OSC` / `XAIR_RETURN_AUDIO`)

Opcional, junto al cliente (`xair_network_bridge_client`) y al servidor (`xair_network_bridge_server`). **No** modifica jitter, FEC ni Opus.

- **OSC write-back** (`XAIR_RETURN_OSC=true`): el cliente escucha OSC estilo DAW (`/track/*/volume|mute|pan|send/*`, `/bus/*/volume`) en `XAIR_RETURN_LISTEN_PORT` (defecto **9002**, para no chocar con `start-reaper-sync` en 9001). Un **hilo de control** hace SET a la mesa (fader, mute, pan, send, bus). `/xremote` actualiza `SyncManager` al momento y reenvía al DAW (`XAIR_RETURN_DAW_*`). `EchoGuard` evita bucles.
- **Audio stereo** (`XAIR_RETURN_AUDIO=true`): el cliente captura 2 canales (DAW) y los envía por `XAIR_RETURN_UDP_PORT` (50001). El servidor los reproduce hacia la USB de la mesa (`XAIR_RETURN_OUTPUT_DEVICE`). Codifica/decodifica en hilos UDP; el callback de play solo saca PCM.
- **Safety** (`XAIR_RETURN_SAFETY=true`, defecto): si el retorno y el mix forward están ambos calientes, silencia el retorno (latch) para cortar un lazo. El snapshot lleva `return_health` y `return_safety`.

### Discovery (`XAIR_DISCOVERY`)

Opcional. Un **hilo de control** (no el callback JACK) escucha mDNS (`224.0.0.251:5353`), SSDP (`239.255.255.250:1900`) y respuestas OSC `/xinfo` (broadcast 10024). El puente anuncia presencia con beacons multicast (`XAIR_DISCOVERY_GROUP` / `XAIR_DISCOVERY_PORT`); el audio UDP **sigue en unicast**. `XAIR_DISCOVERY=false` exige IP manual. Con `XAIR_OSC_HOST=auto` / `XAIR_PEER_HOST=auto` se rellenan desde lo descubierto. CLI: `python -m src.cli discover`.

Opciones CLI: `--reaper-host`, `--reaper-port`, `--listen-port`, `--host` / `--port` (mesa).

**Notas**

- Todo esto es **solo control** sobre la sesión OSC de la mesa (no mueve el proyecto de audio/USB/UDP PCM).
- Firewall / mismo segmento VLAN que la X18 para paquetes hacia UDP **10024**.
