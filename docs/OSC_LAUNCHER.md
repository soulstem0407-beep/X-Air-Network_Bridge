# OSC launcher (X18 ↔ X-Air Network Bridge ↔ REAPER)

Un solo comando configura OSC y deja el sincronizador en marcha. **No** cambia el audio UDP, jitter, FEC, Opus, recorder, el server ni los puertos JACK.

## Qué hace

1. Activa `XAIR_OSC_ENABLED=true` en `.env` si falta.
2. Resuelve `XAIR_OSC_IP` / `XAIR_OSC_HOST` y `XAIR_OSC_PORT` (10024). Si faltan, intenta `/xinfo` en la LAN o pide la IP.
3. Comprueba la mesa: **ping ICMP** + handshake OSC **`/status`**.
4. Comprueba que REAPER escucha OSC (puerto **8000** o `XAIR_REAPER_PORT`).
5. Si `xair_network_bridge_client` no corre, lo arranca (`scripts/xair_network_bridge_client.sh`).
6. Arranca `start-reaper-sync` (faders, mute, pan, nombres, sends, buses, LR).

Mensajes que imprime:

```text
Conectando OSC con X18…
Conectando OSC con REAPER…
Iniciando sincronizador OSC…
OSC conectado correctamente.
```

Estado final: **X18 OK**, **OSC OK**, **REAPER OK**, **sync OK**.

## Cómo usarlo

Desde la raíz del repo:

```bash
bash scripts/launch_osc.sh
```

O:

```bash
source .venv/bin/activate
PYTHONPATH=. python3 -m src.cli osc-launcher
```

Opciones:

| Flag | Efecto |
| --- | --- |
| `--host IP` | IP de la X18 |
| `--port N` | Puerto OSC de la mesa (defecto 10024) |
| `--reaper-host ADDR` | Máquina de REAPER |
| `--reaper-port N` | Puerto donde REAPER escucha OSC |
| `--non-interactive` | No pide IP; falla si no hay host |
| `--no-start-client` | No lanza el cliente de audio |
| `--check` | Solo verifica; no entra en el bucle `start-reaper-sync` |

Ctrl+C detiene el sincronizador (el cliente de audio sigue si ya estaba en marcha).

## Requisitos

- X18 en la LAN, OSC UDP **10024**.
- En `.env` (el launcher lo rellena si puede):
  - `XAIR_OSC_ENABLED=true`
  - `XAIR_OSC_IP` o `XAIR_OSC_HOST` = IP de la mesa
  - `XAIR_OSC_PORT=10024`
  - `XAIR_REAPER_HOST=127.0.0.1` (o la IP del DAW)
  - `XAIR_REAPER_PORT` = puerto local OSC de REAPER (**8000** o el que tengas)
- **REAPER** con Control OSC activo (dos puertos distintos):
  1. Preferences → Control/OSC/Web → **Enable OSC**.
  2. **Local listen port** = `XAIR_REAPER_PORT` (**8000** típico). Ahí REAPER *recibe*.
  3. **Destination IP** = `127.0.0.1` si el launcher está en el mismo PC.
  4. **Destination port** = `XAIR_REAPER_LISTEN_PORT` (**9001**). Ahí el puente *escucha*. No uses 8000 en los dos.
- El cliente del bridge en esta máquina (`xair_network_bridge_client`) para el audio; el launcher lo arranca si falta.

La X18 USB es **`xair_network_bridge_server`** (otra máquina o el mismo host). Este launcher no inicia el server.

## Solución de problemas

**X18 no responde**

- IP mal: `ping <ip>` y `PYTHONPATH=. python3 -m src.cli xair-network-status --host <ip>`.
- Firewall / Wi‑Fi de invitado: OSC es UDP 10024.
- Mesa apagada o en otra VLAN.

**Address already in use / puerto 8000**

- REAPER ya tiene el 8000. El puente debe escuchar en **9001**.
- Local listen = 8000, Destination port = 9001. Relanza `osc-launcher`.

**REAPER no responde**

- OSC no está Enable en Preferences.
- Puerto distinto: pasa `--reaper-port` o edita `XAIR_REAPER_PORT`.
- REAPER en otro PC: `--reaper-host` y abre el UDP en el firewall.

**Puertos incorrectos**

- Mesa: **10024**.
- REAPER *listen* (recibe): **8000** = `XAIR_REAPER_PORT`.
- Puente *listen* (REAPER destination): **9001** = `XAIR_REAPER_LISTEN_PORT`. Nunca el mismo que 8000.

**Cliente no corriendo**

- El launcher lo arranca solo. Si falla JACK: ver `docs/README_JACK_PIPEWIRE.md`.
- `--no-start-client` exige que ya esté vivo.

**Sync no mueve faders**

- Handshake X18 y listen de REAPER deben ser OK.
- Mapa de tracks 1–18 alineado con canales de la mesa.
- Relanza: Ctrl+C y `bash scripts/launch_osc.sh`.

## Ejemplos

```bash
# Todo automático (pide IP si no está en .env)
bash scripts/launch_osc.sh

# IP conocida, REAPER en 8000
PYTHONPATH=. python3 -m src.cli osc-launcher --host 192.168.1.100 --reaper-port 8000

# Solo comprobar, sin bloquear en el sync
PYTHONPATH=. python3 -m src.cli osc-launcher --host 192.168.1.100 --check --non-interactive
```

Detalle del protocolo y variables: `docs/OSC.md`.
