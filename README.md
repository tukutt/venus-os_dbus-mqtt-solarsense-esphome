# dbus-mqtt-solarsense - Bridge a Victron SolarSense 750 (via an ESP32/ESPHome) to the native Venus OS meteo service

<small>GitHub repository: [tukutt/venus-os_dbus-mqtt-solarsense-esphome](https://github.com/tukutt/venus-os_dbus-mqtt-solarsense-esphome)</small>

## Index

1. [Disclaimer](#disclaimer)
1. [Purpose](#purpose)
1. [How it works](#how-it-works)
1. [ESPHome setup (the `mqtt:` component)](#esphome-setup-the-mqtt-component)
1. [Config](#config)
1. [Topic mapping](#topic-mapping-esphome--victron-dbus)
1. [Install / Update](#install--update)
1. [Uninstall](#uninstall)
1. [Restart](#restart)
1. [Debugging](#debugging)
1. [Tests](#tests)
1. [Compatibility](#compatibility)
1. [Credits](#credits)


## Disclaimer

I wrote this script for myself. I'm not responsible if you damage something using my script.


## Purpose

This driver bridges the **Victron SolarSense 750** irradiance sensor to the
**native Victron meteo service** (`com.victronenergy.meteo`) on the Venus OS
dbus. The sensor then appears in the GX Remote Console and on VRM **exactly as
if it had been paired to the Cerbo's own Bluetooth** — except the BLE frames are
decoded by a remote ESP32 instead. This is useful when the SolarSense is **out
of Bluetooth range of the GX device** (e.g. on a roof, in a field, on a separate
building): an ESP32 placed near the sensor relays the data over the network.

Full data path:

```
SolarSense 750  ──BLE──▶  ESP32 (ESPHome decoder + native `mqtt:` component)
                              │  MQTT (publishes directly to the broker)
                              ▼
                        MQTT broker (e.g. Mosquitto)
                              │  MQTT
                              ▼
              this driver (on the Cerbo GX)
                              │  dbus
                              ▼
                   com.victronenergy.meteo  ──▶  Remote Console / VRM
```

> ⚠️ **Read/display only.** The ESP32 is the source of the values. Writing on the
> dbus paths is **not** propagated back.

The companion ESPHome decoder lives in its own repository:
[tukutt/Victron-SolarSense-750-EspHome](https://github.com/tukutt/Victron-SolarSense-750-EspHome).
You add the standard ESPHome `mqtt:` component to it (see below); **Home
Assistant is not required** in this path — the ESP publishes straight to the
broker.


## How it works

ESPHome's `mqtt:` component publishes **one retained topic per entity**, using
the entity platform as the second topic level:

```
<base_topic>/sensor/<object_id>/state          numeric sensors
<base_topic>/text_sensor/<object_id>/state      text sensors (e.g. Error Code)
<base_topic>/binary_sensor/<object_id>/state    binary sensors
<base_topic>/status                             availability LWT: online / offline
```

- `<base_topic>` is the ESPHome **node name** (its `mqtt: topic_prefix`),
  e.g. `esphome-ble-proxy`.
- `<object_id>` is the slugified sensor name, e.g. `solarsense_irradiance`.

The driver:

1. Subscribes to `<base_topic>/+/+/state` — **both** `sensor/` and
   `text_sensor/` levels, because the SolarSense *Error Code* is an ESPHome
   `text_sensor` and would be missed by a `sensor/`-only subscription.
2. Filters on `entity_prefix` (the MQTT `+` wildcard cannot match a partial
   topic level, so the prefix is filtered in Python) — this isolates the
   SolarSense entities from the ESP's own system sensors (uptime, wifi, …).
3. Maps each `object_id` **by suffix** to a `com.victronenergy.meteo` dbus path,
   parses the string payload to a native int/float, and publishes it.
4. Uses the `<base_topic>/status` availability topic to drive `/Connected`
   (when `base_topic` is configured).

The dbus service registers immediately at start-up with idle defaults (all `0`)
and is then populated as messages arrive — **no "minimum message" is required to
start**. Because ESPHome publishes **retained**, the driver receives the full
current state right after it subscribes.


## ESPHome setup (the `mqtt:` component)

Keep your existing `api:` block (so you keep Home Assistant, OTA, logs) and
**add** the `mqtt:` component to your ESPHome YAML:

```yaml
mqtt:
  broker: 192.168.X.X        # your MQTT broker (e.g. Mosquitto)
  port: 1883
  username: !secret mqtt_user    # if your broker requires auth
  password: !secret mqtt_pass
  discovery: false           # IMPORTANT: you already have the entities via the
                             # native API; leave discovery off to avoid duplicate
                             # MQTT-discovered copies in Home Assistant
```

Notes:

- **No `topic_prefix` needed.** By default ESPHome prefixes topics with the
  **node name** (the `name:` under `esphome:`). Just put that node name in the
  driver's `base_topic` — or leave `base_topic` empty and let the driver match
  on `entity_prefix` alone (then renaming the ESP never breaks the driver).
- ESPHome publishes **retained**, so the driver gets the complete current state
  immediately after subscribing.
- After flashing, **check your real topics** before configuring the driver:

  ```bash
  mosquitto_sub -h <broker> -p 1883 -u <user> -P <pass> -t '#' -v
  ```

  You should see e.g. `esphome-ble-proxy/sensor/solarsense_irradiance/state 337.3`
  and `esphome-ble-proxy/status online`.


## Config

Copy or rename `config.sample.ini` to `config.ini` in the `dbus-mqtt-solarsense`
folder and adapt it. Key settings:

| Section | Key | Default | Description |
| --- | --- | --- | --- |
| `[MQTT]` | `broker_address` | `IP_ADDR_OR_FQDN` | IP/FQDN of the MQTT broker the ESP publishes to |
| `[MQTT]` | `broker_port` | `1883` | Broker port |
| `[MQTT]` | `username` / `password` | – | Optional credentials |
| `[MQTT]` | `base_topic` | *(empty)* | ESPHome node name (topic prefix). Empty = wildcard, match on `entity_prefix` only. Set it to also enable the `/status` LWT for `/Connected` |
| `[MQTT]` | `entity_prefix` | `solarsense` | Only object_ids starting with this are bridged |
| `[DEFAULT]` | `device_name` | `SolarSense 750` | Name in Remote Console / VRM |
| `[DEFAULT]` | `device_instance` | `40` | VRM device instance |
| `[DEFAULT]` | `timeout` | `300` | Watchdog (s), `0` to disable |
| `[DEFAULT]` | `logging` | `WARNING` | `ERROR` / `WARNING` / `INFO` / `DEBUG` |

### `/Connected`, robustness & watchdog

`/Connected` is driven by two independent signals:

- The **ESPHome `<base_topic>/status` LWT** (`online`/`offline`) — set
  `base_topic` to enable it. It reflects whether the ESP itself is reachable.
- The **watchdog**: if no SolarSense **data** is received for `timeout` seconds,
  `/Connected` is set to `0`. Only real sensor messages reset the watchdog (the
  `/status` topic does not), so a sensor that is **out of BLE range while the ESP
  stays online** is still caught.

In all cases the **measurements are kept as-is** — `/Irradiance` (and the other
paths) are **not** zeroed. An absence of data is not a measured value of zero.
The SolarSense also reports at night (irradiance `0` is a real measurement), so
the watchdog only ever fires on a genuine outage, **never merely at sunset**.


## Topic mapping (ESPHome → Victron dbus)

Topics are `<base_topic>/<platform>/<object_id>/state`. The `object_id` is
matched on its **suffix** (`-` and `_` treated as equivalent).

| ESPHome sensor | Topic (object_id) | Platform level | Example payload | Victron dbus path | Type / conversion |
| --- | --- | --- | --- | --- | --- |
| SolarSense Irradiance | `solarsense_irradiance` | `sensor` | `337.3` (W/m²) | `/Irradiance` | float, 1 decimal |
| SolarSense Installation Power | `solarsense_installation_power` | `sensor` | `412` (W) | `/InstallationPower` | int |
| SolarSense Today's Yield | `solarsense_today_s_yield` | `sensor` | `0.07` (kWh) | `/TodaysYield` | float, 2 decimals |
| SolarSense Cell Temperature | `solarsense_cell_temperature` | `sensor` | `21.4` (°C) | `/CellTemperature` | float, 1 decimal |
| SolarSense Battery Voltage | `solarsense_battery_voltage` | `sensor` | `3.28` (V) | `/BatteryVoltage` | float, 2 decimals |
| SolarSense Time Since Last Sun | `solarsense_time_since_last_sun` | `sensor` | `330` (min) | `/TimeSinceLastSun` | int |
| SolarSense Error Code | `solarsense_error_code` | **`text_sensor`** | `0x04001405` | `/ErrorCode` | hex/decimal → uint32 |
| SolarSense Charger Error | `solarsense_charger_error` | `text_sensor` | `0x00000000` | *(ignored)* | not bridged |
| Low Battery | `low_battery` | `binary_sensor` | `OFF` | *(ignored)* | not bridged |
| *(ESPHome LWT)* | — | `<base>/status` | `online` | `/Connected` | `online → 1`, `offline → 0` |

The SolarSense does not measure outside temperature, wind speed or wind
direction, so `/ExternalTemperature`, `/WindSpeed` and `/WindDirection` are
intentionally **not** registered (publishing empty paths would show blank fields
in the GUI).

> ℹ️ The exact `object_id` slugs depend on your ESPHome sensor names. The
> apostrophe in "Today's Yield" slugifies unpredictably (`today_s_yield`,
> `todays_yield`, …), and a device name may duplicate the prefix
> (`solarsense_solarsense_irradiance`). Because the mapping matches on the
> **suffix** (and treats `-`/`_` alike), all of these resolve correctly.


## Install / Update

1. Login to your Venus OS device via SSH. See [Venus OS: Root Access](https://www.victronenergy.com/live/ccgx:root_access#root_access) for more details.

2. Execute these commands to download and copy the files:

    ```bash
    wget -O /tmp/download_dbus-mqtt-solarsense.sh https://raw.githubusercontent.com/tukutt/venus-os_dbus-mqtt-solarsense-esphome/master/download.sh

    bash /tmp/download_dbus-mqtt-solarsense.sh
    ```

3. Select the version you want to install.

4. Press enter for a single instance. For multiple instances, enter a number and press enter.

    Example:

    - Pressing enter or entering `1` will install the driver to `/data/etc/dbus-mqtt-solarsense`.
    - Entering `2` will install the driver to `/data/etc/dbus-mqtt-solarsense-2`.

### Extra steps for your first installation

5. Edit the config file to fit your needs. The correct command for your installation is shown after the installation.

    ```bash
    nano /data/etc/dbus-mqtt-solarsense/config.ini
    ```

6. Install the driver as a service:

    ```bash
    bash /data/etc/dbus-mqtt-solarsense/install.sh
    ```

    The daemon-tools should start this service automatically within seconds.


## Uninstall

⚠️ If you have multiple instances, ensure you choose the correct one.

```bash
bash /data/etc/dbus-mqtt-solarsense/uninstall.sh
```


## Restart

```bash
bash /data/etc/dbus-mqtt-solarsense/restart.sh
```


## Debugging

⚠️ If you have multiple instances, ensure you choose the correct one.

Check the logs:

```bash
tail -n 100 -F /data/log/dbus-mqtt-solarsense/current | tai64nlocal
```

The service status can be checked with `svstat /service/dbus-mqtt-solarsense`.

This will output something like `/service/dbus-mqtt-solarsense: up (pid 5845) 185 seconds`.

If the seconds are under 5 then the service crashes and gets restarted all the
time. If you do not see anything in the logs, increase the log level in
`/data/etc/dbus-mqtt-solarsense/config.ini` by setting `logging = INFO` or
`logging = DEBUG`.

If nothing shows up on the meteo device but the ESP is publishing, double-check
the real topics with `mosquitto_sub -t '#' -v` and confirm that `base_topic`
(if set) matches your ESPHome node name and that `entity_prefix` matches the
start of your `object_id`s.

If the script stops with the message
`dbus.exceptions.NameExistsException: Bus name already exists: com.victronenergy.meteo.mqtt_solarsense_40`
it means the service is still running or another service is using that bus name.

### Read settings

Values can be found on this MQTT path of the Venus OS broker:

```
N/<vrm_id>/meteo/40/...
```


## Tests

The ESPHome → dbus translation logic lives in the dependency-free module
`solarsense_parser.py` and is covered by unit tests (no dbus/GLib/paho
required):

```bash
python3 dbus-mqtt-solarsense/test/test_parser.py
```


## Compatibility

This software supports the latest three stable versions of Venus OS. It may also
work on older versions, but this is not guaranteed. It only uses the same
dependencies as the original driver (`paho-mqtt`, bundled, and GLib/dbus from
Venus OS) — no new dependency is added.


## Credits

Based on [tukutt/venus-os_dbus-mqtt-evcc](https://github.com/tukutt/venus-os_dbus-mqtt-evcc),
itself based on [mr-manuel/venus-os_dbus-mqtt-ev-charger](https://github.com/mr-manuel/venus-os_dbus-mqtt-ev-charger)
(MIT licence, retained). The dbus registration via `vedbus`/`VeDbusService`, the
VRM instance handling, the daemon-tools service structure and the install
scripts are kept from those projects. The evcharger MQTT parser is replaced by
the ESPHome native MQTT → `com.victronenergy.meteo` parser.

Companion ESPHome decoder:
[tukutt/Victron-SolarSense-750-EspHome](https://github.com/tukutt/Victron-SolarSense-750-EspHome).
