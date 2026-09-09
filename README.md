# enphase2mqtt

Bridges an **Enphase IQ Gateway (Envoy)** to MQTT, with Home Assistant style
discovery — **including one device per solar panel**.

Everything is read from the gateway **on your LAN**. The Enphase cloud is
contacted once, to obtain the local access token, which is then stored and
renewed automatically.

Built on [pyenphase](https://github.com/pyenphase/pyenphase), the library behind
the official Home Assistant Enphase integration.

Not affiliated with, endorsed by, or supported by Enphase Energy.

## Why

Most Envoy-to-MQTT bridges republish `/ivp/meters/readings` and stop there: you
get the production and consumption totals, and nothing about the panels
themselves. The endpoint that carries per-microinverter data is a different one,
and it carries far more than a wattage.

This bridge publishes **DC voltage, DC current and temperature for every
panel**. That is what lets you see a panel that is shaded, dirty or failing —
long before the total production says anything at all.

## Features

- **One device per microinverter.** Power, energy today, lifetime energy, DC
  voltage, DC current, temperature.
- **One device per phase** on a three phase installation.
- **The meters**, with the things the aggregate view usually drops: power
  factor, current, grid voltage and frequency, and the metering status of each
  CT — the "a current transformer has dropped out" signal.
- **Curated, not dumped.** Around 105 entities on a three phase installation
  with ten panels, and every one of them carries a value. Aggregates the gateway
  leaves permanently at zero are not published, and grid voltage and frequency
  are published once rather than once per meter.
- **Local and read-only.** No cloud polling, no commands, nothing written to the
  gateway.
- **Robust.** Discovery and states are replayed on every reconnection to the
  broker; an MQTT Last Will reports the bridge going down; a gateway reboot
  flips the bridge to offline and it retries rather than dying.

## What it publishes

| Device | Entities | Contents |
| --- | --- | --- |
| Envoy | 24 | production, consumption and net consumption (now, today, 7 days, lifetime), power factor and current of both CTs, grid voltage and frequency, metering status and fault flags, link indicator |
| Envoy phase L1/L2/L3 | 7 each | production and consumption (now and today), net consumption, voltage, power factor |
| One per panel | 6 each | power, energy today, lifetime energy, DC voltage, DC current, temperature |

Aggregates the gateway does not compute are deliberately absent: the Envoy
reports `0` for net consumption "today" and "last 7 days", so publishing them
would create entities that never hold anything.

## Requirements

- An MQTT broker.
- The LAN address of a commissioned Enphase IQ Gateway.
- Your Enlighten account — the one used by the Enphase mobile app. On firmware 7
  and above the local API needs a token, and the account is what obtains and
  renews it. An owner token is valid for one year; the bridge renews it a month
  before it expires.

## Running it

### Docker

```
docker run -d --name enphase2mqtt \
  -e ENVOY_HOST=192.168.1.50 \
  -e ENVOY_USER=you@example.com \
  -e ENVOY_PASSWORD='your-enlighten-password' \
  -e MQTT_HOST=192.168.1.10 \
  -e MQTT_USER=mqtt -e MQTT_PASSWORD='mqtt-password' \
  -v /path/to/config:/config \
  ghcr.io/ripleyxlr8/enphase2mqtt:latest
```

Mounting `/config` is strongly recommended: it is where the access token is
stored, and without it every restart asks Enphase for a new one.

### Unraid

Available in Community Applications. The template is
`enphase2mqtt-unraid-template.xml` in this repository.

### Configuration

Every setting can come from an environment variable or from
`/config/enphase2mqtt.conf`; the environment wins. See
[`enphase2mqtt.conf.template`](enphase2mqtt.conf.template), which documents
every option and gives the matching variable name.

Useful ones:

- `ENVOY_POLL_INTERVAL` (default 60s). The CT meters are near real time, but
  microinverters only report every five minutes or so — polling faster mostly
  adds load to the gateway.
- `ENVOY_PANEL_NAMES` maps a microinverter serial number to a name of your
  choice. Without it panels are named `Panneau 1`, `Panneau 2`… in ascending
  serial order, which will **not** necessarily match the numbering of an
  integration you are replacing.
- `ENVOY_PUBLISH_PANELS` / `ENVOY_PUBLISH_PHASES` turn whole blocks off.

## Topics

```
<prefix>/<serial>/link/state                   ON | OFF
<prefix>/<serial>/<entity>/state               gateway entities
<prefix>/<serial>/phase_l1/<entity>/state      per phase
<prefix>/<serial>/panneau/<inverter>/<e>/state per panel
<discovery_prefix>/sensor/<device>/<e>/config  discovery
```

`link/state` is both the link indicator and the availability topic of every
other entity. That is deliberate: MQTT allows **one** Last Will per connection,
so a bridge that keeps availability and a link indicator on two separate topics
can only protect one of them — kill it and the other stays stuck on "online",
lying until the next start. One topic, carried by the Will, cannot diverge.

## Jeedom notes

- The **state topic prefix** (`enphase` by default) must be listed in the *data
  topics* setting of the MQTT Discovery plugin, or the plugin ignores the
  discovery messages. You do not have to type it: once its daemon has run, the
  plugin offers the roots it has discovered with a `+` button.
- Each device becomes its own Jeedom equipment: one `Envoy`, three phases, and
  one per panel. Put them in the object of your choice and they group naturally.
- **Command names**: older MQTT Discovery releases let `device_class` overwrite
  the published name. Use a release where the received `name` wins, otherwise
  several entities arrive with a generic label.
- Jeedom's core **strips the ASCII apostrophe** from any command name, on any
  plugin. No entity name here contains one, and a test enforces that.
- `Anomalies compteur …` is published as text (`-` when there is nothing). If
  you would rather have an alert tile, convert it yourself and remember that the
  `core::alert` widget shows **red on 0 and green on 1** — a problem-style
  command needs `invertBinary`.

## Home Assistant notes

Discovery is standard, so the devices appear on their own. Energy entities are
published with `state_class: total_increasing` and are usable in the Energy
dashboard: production from `Production totale`, grid import and export from
`Énergie soutirée` and `Énergie injectée`.

## Tests

The entity tables are verified offline — no broker, no gateway, no credentials.
The fixture reproduces the real shape of a three phase `EnvoyData` with ten
microinverters.

```
python tests/test_mapping.py
```

They check the things that actually bite: no duplicate name inside a device
(Jeedom silently merges two commands of the same name and deletes one), no ASCII
apostrophe, no entity that can never hold a value, and no grandeur published
twice. Each of those protections has been verified by mutation — reintroducing
the defect makes the suite fail.

## License

MIT.
