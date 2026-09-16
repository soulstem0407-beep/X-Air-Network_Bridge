# Sincronización OSC y audio UDP

Este proyecto mueve audio multicanal por UDP mientras que el estado de mezcla (nombres, faders, preamps, meters) vive en la mesa **X AIR** según protocolo OSC. El módulo `src/sync/sync_manager.py` mantiene un **estado unificado sólo en memoria** para comparar dos fuentes sin alterar ni el códec UDP ni los mensajes OSC.

## ¿Qué se sincroniza?

| Origen | Qué se registra |
|--------|-----------------|
| **OSC** (`XAirOSCBridge`) | Nombre de canal, fader (`/mix/fader`), ganancia HA (`get_gain`), entero del meter `/meters/1` como `meter_osc`, y marca `last_osc_ns`. |
| **Audio UDP + receptor** | RMS lineal por canal en el momento del datagrama, tomado del bloque decodificado, y marca `last_audio_ns` usando `ts_ns` de la cabecera del paquete. |
| **Métricas del receptor** | Jitter EMA/pico, pérdidas, underruns, target adaptive, `rx_health`, FEC, Opus (`opus_enabled`, `opus_bitrate`, `opus_frame`, `opus_decode_errors`) vía `set_metrics_provider` → `NetworkReceiver.health_snapshot()`. |
| **Return path** | `report.return_path`: OSC write-back y audio stereo de retorno (`enabled`, `return_health`, `return_safety`, `last_writeback_ns`). |
| **Discovery** | `report.discovery`: `discovery_enabled`, `discovered_devices[]` (name, model, ip, firmware), `last_discovery_ts`, `discovery_health`. También `.xair_discovery.json`. |
| **EQ / dyn / FX** | `report.dsp`: `eq_enabled` / `dyn_enabled` / `fx_enabled`, `eq_health` / `dyn_health` / `fx_health` (`off`/`ok`/`warn`/`fail`), `last_*_write_ns`, `last_change`. Por canal: `last_eq_write_ns`, `last_dyn_write_ns`, `last_fx_write_ns`. |
| **Recorder** | `report.record`: `record_enabled`, `recording`, `record_path`, `record_files[]`, `last_record_ts`, `record_health`. También `.xair_record.json`. |
| **Tests** | `report.test`: `last_test_run` (unix), `test_health` (`off`/`ok`/`warn`/`fail`), `tests_run` / `tests_failed` / `tests_errors` / `tests_skipped`. También `.xair_test.json` (CLI `test`). |
| **Service** | `report.service`: `service_enabled`, `last_service_action` (`install`/`remove`). También `.xair_service.json` (CLI `install-service` / `remove-service`). |
| **Package** | `report.package`: `package_version`, `package_build_ts`. También `.xair_package.json` (CLI `package-deb` / `package-tar`). |
| **Release** | `report.release`: `release_version`, `release_build_ts`. También `.xair_release.json` (CLI `build-release`). |
| **Integration** | `report.integration` / `report.integration_health` / `report.last_integration_ts`: peor salud de los módulos *activos* (RX, FEC, Opus, return, discovery, EQ/DYN/FX, recorder). `off` si el cliente no está activo. |

No existe reloj común tipo PTP entre la consola y el PC salvo que añadas tú uno. Por tanto **`ts_ns`** del lado emisor y **`time.time_ns()`** del lado OSC no son necesariamente el mismo Epoch; úsalos sobre todo para **orden relativo** y depuración, no como medida absoluta de latencia de red/consola sin calibración adicional.

## Drift temporal

- **`compute_drift_ns(ch)`** devuelve `last_audio_ns - last_osc_ns` cuando ambos existen para ese canal, y si no hay datos `None`.
- **`compute_global_drift_ns()`** es la **media entera redondeada** de esos deltas entre los canales configurados (`XAIR_CHANNELS` / parámetro `channels` del `SyncManager`) que tengan par válido.

La interpretación del signo depende de cómo relacionen esos dos relojes; el valor es ante todo una **métrica de coherencia** entre “último paquete visto” y “último refres OSC de ese canal”, no garantía absoluta entre consola y host.

## Modo automático en el cliente

Con **`XAIR_SYNC_ON_CLIENT=true`** en `.env`, el comando **`python -m src.cli xair_network_bridge_client`** (vía `src/client/start_client.py`) hace lo siguiente sin tocar la API de `NetworkReceiver` más que pasar el hook opcional:

1. **`SyncManager.from_env(channels=XAIR_CHANNELS)`** — mismo número de canales que el receptor.
2. **`audio_levels_hook=sm.update_audio_levels`** — RMS por canal y `ts_ns` tras cada UDP decodificado (igual que un hook manual).
3. **`sm.periodic_refresh(intervalo)`** — hilo daemon que cada **`XAIR_SYNC_REPORT_INTERVAL_SEC`** llama **`refresh_osc_state()`** y actualiza un **snapshot JSON** en disco (`XAIR_SYNC_STATE_PATH` si está definido, si no un fichero `.xair_sync_state.json` en la raíz del proyecto de trabajo).

Las órdenes **`sync-status`**, **`sync-detail`** y **`dashboard`** (otra terminal/proceso) **leen ese snapshot**. El dashboard es TUI por defecto (`python -m src.cli dashboard`) o HTTP local (`--http`, bind 127.0.0.1:8765). Terminar el cliente con **Ctrl+C** marca el archivo como **`"active": false`** y conserva el último `report` (métricas, canales, `integration_health=off`) para que el dashboard no se quede en blanco.

El cliente también registra **`receiver_metrics`** (health, jitter EMA/pico, pérdidas, underruns, target del jitter adaptive, cola) en el snapshot mediante `SyncManager.set_metrics_provider`, invocado en la cadencia de escritura (~0.25 s), no en cada datagrama UDP. `buffer_fill_percent_ema` es el llenado **respecto al target** de playout (~100 % = en consigna).

Otros procesos pueden inspeccionar el mismo proceso con **`get_running_sync_manager()`** en `src.client.start_client` (devuelve `None` si el modo sync no está activo).

## Comparación de niveles

- RMS del receptor se convierte a **dBFS** \(20 log₁₀(linear\_rms)\) con umbral numérico bajo contra log(0).
- El meter OSC se interpreta como **dB escalados** usando la convención habitual del firmware: **`dB_meter = meter_int / 256.0`** (documentación MUSIC Group sobre resolución del medidor).

**`delta`** es `abs(dBFS_rms - dB_meter)`. **OK** cuando `delta ≤ XAIR_SYNC_LEVEL_TOLERANCE_DB` (por defecto **6 dB**).

Los medidores de consola no miden exactamente lo mismo que un RMS puntual sobre un bloque UDP (ventana diferente, posición del tap, ballistics, trims, etc.). La tolerancia es **configurable por .env** a propósito.

## API principal (`SyncManager`)

- `refresh_osc_state()` — Recorre los canales 1…N configurados en la mesa y aplica los cambios al estado OSC bajo un candado único tras la I/O (sin bloquear actualizaciones RMS durante las peticiones OSC).
- `periodic_refresh(interval_sec)` — Hilo daemon que en cada ciclo llama `refresh_osc_state()` y, si está configurada ruta de snapshot, vuelca el informe (`build_sync_report()`) a disco.
- `update_audio_levels(ch_rms: dict[int, float], ts_ns: int)` — Lo puede invocar el receptor vía **`audio_levels_hook`** (ver abajo).
- `set_metrics_provider(fn)` — callback barato para jitter/pérdidas al volcar el JSON (el cliente lo conecta a `ReceiverMetrics.snapshot()`).
- `ingest_receiver_metrics(snapshot)` — copia explícita de jitter/buffer/perdidas si lo necesitas fuera del provider.
- `build_sync_report()` — Devuelve un `dict` (sin imprimir por consola) con `global_drift_ns`, filas por canal (`delta_level`, `level_ok`, `drift_ns`, …), y metadatos.

## Hook en el receptor

`NetworkReceiver` acepta un argumento opcional:

```python
audio_levels_hook: Optional[Callable[[dict[int, float], int], None]] = None
```

Tras cada datagrama decodificado con éxito, si el hook está definido y las dimensiones coinciden, se calculan RMS por canal sobre **ese bloque** y se llama `hook(ch_rms, int(hdr.ts_ns))`. La lógica de jitter, pérdidas y reproducción **no cambia**.

Ejemplo típico (mismo proceso que el cliente):

```python
from src.sync.sync_manager import SyncManager
from src.network_receiver.receiver import NetworkReceiver

sm = SyncManager.from_env()

recv = NetworkReceiver(
    ...,
    audio_levels_hook=sm.update_audio_levels,
)
```

Para añadir `ingest_receiver_metrics` usando el mismo receptor:

```python
class _Box:
    recv: NetworkReceiver


box = _Box()

def hook(rms: dict[int, float], ts_ns: int) -> None:
    sm.update_audio_levels(rms, ts_ns)
    sm.ingest_receiver_metrics(box.recv.metrics.snapshot())


box.recv = NetworkReceiver(..., audio_levels_hook=hook)
```

## CLI

Desde la raíz del proyecto (véase también `docs/README.md`):

```bash
PYTHONPATH=. python3 -m src.cli sync-status
PYTHONPATH=. python3 -m src.cli sync-detail
PYTHONPATH=. python3 -m src.cli dashboard
PYTHONPATH=. python3 -m src.cli dashboard --http
PYTHONPATH=. python3 -m src.cli test
```

Con **`XAIR_SYNC_ON_CLIENT=true`** y el cliente en marcha:

- **`sync-status`**: drift global medio, número de canales con drift válido y resumen OK/KO usando el **snapshot** escrito por el proceso `xair_network_bridge_client` (véase modo automático arriba). Incluye EQ/DYN/FX health, `integration_health` / `last_integration_ts`, y el último cambio de parámetro.
- **`sync-detail`**: tabla por canal (fader, gain, RMS, meter OSC, ΔdB, drift, last EQ/DYN/FX write, ok) a partir del mismo snapshot.
- **`dashboard`**: TUI en vivo (q/ESC para salir, **r** start/stop de grabación). **`dashboard --http`**: página local en `http://127.0.0.1:8765/` (bind localhost; botones Record start/stop).
- **`test`**: corre la suite unittest **sin mesa ni JACK**. Imprime un resumen pass/fail y persiste `last_test_run` / `test_health` en `.xair_test.json` (y en `report.test` si el snapshot del cliente está activo). Exit ≠ 0 si hay failures o errors. `warn` si hay skips (p. ej. Opus sin `libopus`).
- **`install-service` / `remove-service`**: unidades systemd de usuario (o `--system`). `systemctl --user reload` envía SIGHUP; `stop` envía SIGTERM (cierre ordenado). No modifica jitter/FEC/Opus. Sidecar `.xair_service.json` (`service_enabled`, `last_service_action`). En el host de la X18 usa `--role server`; en el DAW `--role client`. Arranque al boot sin login: `--linger`.
- **`package-deb` / `package-tar`**: generan `dist/*.deb` y `dist/*.tar.gz` (unidades systemd, Depends, wrapper `xair-network-bridge`). Escriben `package_version` y `package_build_ts`. No cambian jitter/FEC/Opus.
- **`build-release`**: mismo `.deb` y tarball más unidades systemd en `dist/systemd/`, `RELEASE` (versión + flags `-O2`/`strip`), `SHA256SUMS`, y si hay `cc` un launcher ELF stripped en `dist/bin/xair-network-bridge`. `SOURCE_DATE_EPOCH` fija timestamps. Snapshot `release_version` / `release_build_ts`. No modifica transporte ni control OSC.

Sin cliente activo o sin snapshot **válido reciente**, ambas ordenes muestran:

```text
SyncManager no está activo. Activa XAIR_SYNC_ON_CLIENT=true en .env.
```

La antigüedad máxima del snapshot admisible escala con **`XAIR_SYNC_REPORT_INTERVAL_SEC`** (unos segundos de margen además del intervalo de refresco). CLI y dashboard usan la misma función (`snapshot_fresh`): la edad sale de **`exported_time_ns`** cuando está en el JSON, si no del mtime del fichero. HTTP y TUI refrescan al mismo intervalo (`XAIR_DASHBOARD_INTERVAL_SEC`, 0.25 s), alineado con la cadencia de escritura del snapshot.

## Recorder WAV (`XAIR_RECORD`)

Sidecar en `xair_network_bridge_client`: un hilo `xair-record` escribe un WAV PCM24 **por canal** bajo `XAIR_RECORD_PATH`. El hilo UDP solo hace `copy` + `put_nowait` (descarta si la cola está llena). El callback JACK/PipeWire no toca disco. `XAIR_RECORD=true` abre sesión al arrancar; `record-start` / `record-stop` (y el dashboard) conmutan en caliente vía `.xair_record_cmd`.


## Variables de entorno

| Variable | Por defecto | Uso |
|----------|-------------|-----|
| `XAIR_SYNC_ON_CLIENT` | `false` | Activa `SyncManager`, hook de RMS y `periodic_refresh` en `xair_network_bridge_client`. |
| `XAIR_SYNC_STATE_PATH` | (no definido: `.xair_sync_state.json` en raíz habitual) | Ruta del JSON para `sync-status` / `sync-detail` / `dashboard`. |
| `XAIR_DASHBOARD_INTERVAL_SEC` | `0.25` | Refresco de la TUI. |
| `XAIR_SYNC_LEVEL_TOLERANCE_DB` | `6.0` | Máximo `Δ` permitido entre dBFS (RMS) y dB escalados meter OSC. |
| `XAIR_SYNC_REPORT_INTERVAL_SEC` | `5.0` | Periodo entre refrescos OSC automáticos y caducidad aproximada del snapshot en la CLI. |
| `XAIR_EQ` / `XAIR_DYN` / `XAIR_FX` | `false` | Control OSC de EQ, gate/compresor y FX en el cliente (hilo de control; ver `docs/OSC.md`). |
| `XAIR_RECORD` | `false` | Sidecar WAV por canal al arrancar el cliente. |
| `XAIR_RECORD_PATH` | `recordings/` | Directorio de sesiones (`YYYYMMDDTHHMMSSZ/*.wav`). |
| `XAIR_TEST_STATE_PATH` | (no definido: `.xair_test.json` en la raíz) | Sidecar JSON de la última corrida `python -m src.cli test`. |
| `XAIR_SERVICE_STATE_PATH` | (no definido: `.xair_service.json` en la raíz) | Sidecar JSON de `install-service` / `remove-service`. |
| `XAIR_PACKAGE_STATE_PATH` | (no definido: `.xair_package.json` en la raíz) | Sidecar JSON de `package-deb` / `package-tar`. |
| `XAIR_RELEASE_STATE_PATH` | (no definido: `.xair_release.json` en la raíz) | Sidecar JSON de `build-release`. |
| `SOURCE_DATE_EPOCH` | (unix now) | Timestamps reproducibles en `build-release` / empaquetado. |

## Limitaciones

- Sin calibración de reloj compartido, **`drift_ns` es sólo diferencia entre marcas locales de dos subsistemas**.
- RMS “por paquete” vs meter de mesa pueden divergir aun cuando el sistema esté sano → no uses la comparación como test binario de corrupción de audio sin ajustar tolerancia y proceso.
- `seq` del protocolo UDP no se almacena en `SyncChannelState`; si lo necesitas, extiende el hook o el estado sin tocar el camino de reproducción.
