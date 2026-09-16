# Latencia extremo‑a‑extremo en LAN

Objetivo: minimizar tamaño de buffer **sin romper por jitter** UDP (sin retransmitir paquetes).

## Parámetros clave (.env)

| Variable | Rol |
|----------|-----|
| `XAIR_SAMPLES_PER_PACKET` | Muestras por canal por datagrama. Con **PCM24** y **18 canales**, el valor por defecto **21** deja el payload ≈ 1134 bytes + cabecera (28 B) ≈ 1162 B, habitualmente sin fragmentar en Ethernet MTU 1500. Valores mayores reducen la cabecera relativa por segundo útil; valores menores acercan el bloque a menos milisegundos de audio por paquete (`n / 48000`). |
| `XAIR_JITTER_PACKETS` | Suelo del prebuffer (y valor fijo si adaptive está off). Orientativo **4–6 en LAN** por cable. |
| `XAIR_JITTER_ADAPTIVE` | `true` (defecto): el receptor sube/baja el target según `jitter_ms_ema` y underruns. `false` = fijo. |
| `XAIR_JITTER_MAX` | Techo en paquetes (defecto **16**). `XAIR_JITTER_MIN` opcional (si falta, = `XAIR_JITTER_PACKETS`). |
| `XAIR_JITTER_COVER` | Factor de cobertura: target ≈ ceil(cover × jitter_ema / duración_paquete). Defecto **3**. |
| `XAIR_FEC` | `true` activa XOR FEC (un datagrama de paridad por grupo). `false` (defecto) = UDP crudo. Activar en **emisor y receptor**. |
| `XAIR_FEC_GROUP` | Tamaño de grupo (defecto **3**, rango 2–16). El receptor reconstruye **una** pérdida por grupo. Añade ~N−1 paquetes de espera en el hilo UDP, no en el callback de audio. |
| `XAIR_OPUS` | `true` envía Opus (hilo UDP send) en lugar de PCM. `false` (defecto) = PCM/FLAC. Activar en **ambos** extremos. |
| `XAIR_OPUS_BITRATE` | Bitrate **total** (defecto **128000**), repartido entre pares estéreo. |
| `XAIR_OPUS_FRAME` | Duración de frame Opus en ms (2.5/5/10/**20**/40/60). Típico 10–20. |
| `XAIR_CODEC` | `PCM24` mínimo CPU; FLAC sube trabajo y retardos de codificación. |
| `XAIR_METRICS_INTERVAL_SEC` | Cada cuántos segundos imprime pérdidas, **jitter_ms_ema**, `buffer_fill_ema`. `0` desactiva (excepto al parar cliente). |

## PortAudio (`sounddevice`)

- Servidor (**entrada**): `latency` explícito ≈ `2 × blocksize / Fs` + `prime_output_buffers_using_stream_callback=False` donde exista API.
- Cliente (**salida**): igual + `WasapiSettings(exclusive=true)` sólo si `XAIR_WASAPI_EXCLUSIVE` (Windows exclusivo suele ganar algunos ms pero puede ocupar solo el cable).

La cabecera UDP ya lleva **`seq`** (órden **`ts_ns`** lado emisor). El receptor calcula jitter **local** contrastando tiempo entre llegadas monotónico con `nframes/Fs`; `ts_ns` sirve sólo offline (los relojes no están sincronizados tipo PTP).

## Pérdidas e interpolación

Si llega seq `earliest` y falta uno o más valores previos, se generan muestras con **interpolación lineal** canal a canal entre la última fila válida conocida (`tail`) y la primera fila de `earliest`.

## Jitter adaptive (receptor)

El hilo UDP (no el callback de audio) revisa métricas cada ~0.5 s. El target cubre `XAIR_JITTER_COVER × jitter_ms_ema` en paquetes, acotado a `[suelo, XAIR_JITTER_MAX]`. Crece de uno en uno (o más si hay underrun); baja sólo tras varios periodos quietos. Al crecer, el callback inserta un hueco corto de silencio (`hold`) para dejar que la cola suba; al bajar, descarta como máximo un paquete extra por callback. `XAIR_JITTER_PACKETS` / `set-buffer` es el **suelo**. El camino UDP no cambia de protocolo.

## FEC XOR (opcional)

Con `XAIR_FEC=true` el emisor agrupa `XAIR_FEC_GROUP` paquetes de media y envía uno de paridad (XOR). El hilo UDP del receptor retiene el grupo el tiempo de llegar la paridad; si falta **exactamente uno**, lo reconstruye y lo entrega al reorder/jitter. Dos o más pérdidas en el mismo grupo son irrecuperables (sigue la interpolación lineal). La paridad **no** ocupa `seq` de audio. `XAIR_FEC=false` ignora datagramas con flag FEC y no espera grupos. El jitter adaptive no cambia.

## Opus (opcional)

Con `XAIR_OPUS=true` el emisor encola PCM desde el callback de captura y **codifica en el hilo UDP send**. El receptor **decodifica en el hilo UDP recv** a PCM y luego entra al reorder/jitter igual que PCM. El callback JACK/PipeWire no ve Opus. Datagramas con `FLAG_OPUS` se descartan si el receptor tiene Opus off. El seq de media sigue incrementando (no es un canal paralelo tipo FEC). FEC XOR, si está on, opera sobre el payload Opus sin cambios de lógica.

## Heurística de ajuste (LAN cable)

1. Fija MTU estable y `XAIR_SAMPLES_PER_PACKET≈21` (PCM24, 18 ch).
2. Con servicio estable, deja **`XAIR_JITTER_PACKETS=5`** (suelo). Adaptive crece si hay underruns o jitter alto; `set-buffer` cambia el suelo.
3. Si no quieres que se mueva solo: **`XAIR_JITTER_ADAPTIVE=false`**. Si hay cortes, prueba suelo **6**.
4. Desactiva Wi‑Fi (usar RJ45) antes de cualquier tuning fino — el jitter de radio domina antes que el tamaño del paquete.
5. Revisa **`métricas RX`** / dashboard: `rx_health`, FEC, Opus (`opus_health`, `opus_decode_errors`), Return (`return_health`, `return_safety`, `last_writeback_ns`), EQ/DYN/FX (`eq_health` / `dyn_health` / `fx_health`), Record (`record_health`, `last_record_ts`) e **`integration_health` / `last_integration_ts`**.

## Resumen físico inevitable

Cadena típica: buffer captura → kernel UDP → receptor → jitter → OutputStream → driver virtual → DAW → cada uno añade muestras. El tamaño efectivo audible es la suma; este proyecto reduce lo que puede **user-space** pero no elimina buffering del SO/driver.
