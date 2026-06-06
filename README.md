# dbus-mqtt-solarsense - Bridge a Victron SolarSense 750 (via ESPHome + Home Assistant) to the native Venus OS meteo service

<small>GitHub repository: [tukutt/venus-os_dbus-mqtt-solarsense](https://github.com/tukutt/venus-os_dbus-mqtt-solarsense)</small>

## Index

1. [Disclaimer](#disclaimer)
1. [Purpose](#purpose)
1. [How it works](#how-it-works)
1. [Home Assistant setup (MQTT Statestream)](#home-assistant-setup-mqtt-statestream)
1. [Config](#config)
1. [Topic mapping](#topic-mapping-esphome--home-assistant--victron-dbus)
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
SolarSense 750  ──BLE──▶  ESP32 (ESPHome decoder, native HA API)
                              │
                              ▼
                       Home Assistant
                              │  MQTT Statestream integration
                              ▼
                  Mosquitto broker (on HA)
                              │  MQTT
                              ▼
              this driver (on the Cerbo GX)
                              │  dbus
                              ▼
                   com.victronenergy.meteo  ──▶  Remote Console / VRM
```

> ⚠️ **Read/display only.** The SolarSense (through Home Assistant) is the sole
> master of the values. Writing on the dbus paths is **not** propagated back.

The companion ESPHome decoder lives in its own repository:
[tukutt/Victron-SolarSense-750-EspHome](https://github.com/tukutt/Victron-SolarSense-750-EspHome).
**The ESP32 is not modified by this project** — it stays on the native Home
Assistant API; Home Assistant is what republishes the entities over MQTT.


## How it works

1. The ESP32 decodes the SolarSense BLE advertisements and exposes them to Home
   Assistant over the **native ESPHome API** (no `mqtt:` component on the ESP).
2. Home Assistant's **MQTT Statestream** integration republishes each entity to
   its Mosquitto broker as one retained topic per entity:

   ```
   <base_topic>/sensor/<entity_id>/state          payload = value string
   <base_topic>/binary_sensor/<entity_id>/state   payload = "on"/"off"
   ```

   e.g. `statestream/sensor/solarsense_irradiance/state` → `337.3`
3. This driver subscribes to `<base_topic>/sensor/+/state` on the HA broker,
   filters on the configured `entity_prefix` (MQTT wildcards cannot match a
   partial topic level, so the prefix is filtered in Python), maps each
   entity_id **by suffix** to a `com.victronenergy.meteo` dbus path, parses the
   string payload to a native int/float, and publishes it on the dbus.

The dbus service registers immediately at start-up with idle defaults (all `0`)
and is then populated as messages arrive — **no "minimum message" is required to
start**. Because Statestream publishes **retained**, the driver receives the
full current state right after it subscribes.


## Home Assistant setup (MQTT Statestream)

The ESP32 stays on the native HA API — **no ESPHome change is needed**. You only
have to tell Home Assistant to republish the SolarSense entities over MQTT.

Add this to Home Assistant's `configuration.yaml` to publish **only** the
SolarSense entities (do not enable Statestream for *all* entities — that would
flood the broker):

```yaml
mqtt_statestream:
  base_topic: statestream
  include:
    entity_globs:
      - sensor.solarsense_*
```

Then **restart Home Assistant** for the block to take effect.

Notes:

- **Check your real entity_ids.** Open Home Assistant → *Developer Tools →
  States* and confirm the actual entity_ids of your SolarSense entities (they
  depend on the ESPHome device/sensor names). Adapt the `entity_globs` above
  and/or the `entity_prefix` in `config.ini` accordingly. The driver matches on
  the **suffix** of the entity_id, so a duplicated prefix
  (`sensor.solarsense_solarsense_irradiance`) still works.
- **Statestream publishes retained**, so the driver receives the complete
  current state immediately after subscribing — even across driver restarts.
- The `binary_sensor` (Low Battery) is **not** bridged; the meteo service has no
  path for it. The `Charger Error` text sensor is intentionally **ignored** as
  well (only `Error Code` is mapped to `/ErrorCode`).


## Config

Copy or rename `config.sample.ini` to `config.ini` in the `dbus-mqtt-solarsense`
folder and adapt it. Key settings:

| Section | Key | Default | Description |
| --- | --- | --- | --- |
| `[MQTT]` | `broker_address` | `IP_ADDR_OR_FQDN` | IP/FQDN of the Home Assistant Mosquitto broker |
| `[MQTT]` | `broker_port` | `1883` | Broker port |
| `[MQTT]` | `username` / `password` | – | Optional credentials |
| `[MQTT]` | `base_topic` | `statestream` | HA Statestream `base_topic` |
| `[MQTT]` | `entity_prefix` | `solarsense` | Only entity_ids starting with this are bridged |
| `[DEFAULT]` | `device_name` | `SolarSense 750` | Name in Remote Console / VRM |
| `[DEFAULT]` | `device_instance` | `40` | VRM device instance |
| `[DEFAULT]` | `timeout` | `300` | Watchdog (s), `0` to disable |
| `[DEFAULT]` | `logging` | `WARNING` | `ERROR` / `WARNING` / `INFO` / `DEBUG` |

### Robustness / Watchdog

- If no SolarSense message is received for `timeout` seconds, the ESP32 is down
  or out of BLE range, so `/Connected` is set to `0`.
- **The measurements are kept as-is** — `/Irradiance` (and the other paths) are
  **not** zeroed. An absence of data is not a measured value of zero.
- When messages resume, `/Connected` returns to `1`.
- The SolarSense also reports at night (irradiance `0` is a real measurement),
  so the watchdog only ever fires on a genuine outage, **never merely at
  sunset**.


## Topic mapping (ESPHome → Home Assistant → Victron dbus)

All topics are relative to `<base_topic>/sensor/` (default `statestream/sensor/`)
and end in `/state`. The entity_id is matched on its **suffix**.

| ESPHome sensor | HA entity_id (typical) | Example payload | Victron dbus path | Type / conversion |
| --- | --- | --- | --- | --- |
| SolarSense Irradiance | `sensor.solarsense_irradiance` | `337.3` (W/m²) | `/Irradiance` | float, 1 decimal |
| SolarSense Installation Power | `sensor.solarsense_installation_power` | `412` (W) | `/InstallationPower` | int |
| SolarSense Today's Yield | `sensor.solarsense_today_s_yield` | `0.07` (kWh) | `/TodaysYield` | float, 2 decimals |
| SolarSense Cell Temperature | `sensor.solarsense_cell_temperature` | `21.4` (°C) | `/CellTemperature` | float, 1 decimal |
| SolarSense Battery Voltage | `sensor.solarsense_battery_voltage` | `3.28` (V) | `/BatteryVoltage` | float, 2 decimals |
| SolarSense Time Since Last Sun | `sensor.solarsense_time_since_last_sun` | `330` (min) | `/TimeSinceLastSun` | int |
| SolarSense Error Code | `sensor.solarsense_error_code` | `0x04001405` | `/ErrorCode` | hex/decimal → uint32 |
| SolarSense Charger Error | `sensor.solarsense_charger_error` | `0x00000000` | *(ignored)* | not bridged |
| Low Battery | `binary_sensor.low_battery` | `off` | *(ignored)* | not bridged |
| *(driver-managed)* | — | — | `/Connected` | `1` = data flowing, `0` = sensor offline |

The SolarSense does not measure outside temperature, wind speed or wind
direction, so `/ExternalTemperature`, `/WindSpeed` and `/WindDirection` are
intentionally **not** registered (publishing empty paths would show blank fields
in the GUI).

> ℹ️ The exact entity_id slugs depend on your ESPHome device/sensor names. The
> apostrophe in "Today's Yield" slugifies unpredictably (`today_s_yield`,
> `todays_yield`, …), and an ESPHome device name may duplicate the prefix
> (`solarsense_solarsense_irradiance`). Because the mapping matches on the
> **suffix**, all of these resolve correctly.


## Install / Update

1. Login to your Venus OS device via SSH. See [Venus OS: Root Access](https://www.victronenergy.com/live/ccgx:root_access#root_access) for more details.

2. Execute these commands to download and copy the files:

    ```bash
    wget -O /tmp/download_dbus-mqtt-solarsense.sh https://raw.githubusercontent.com/tukutt/venus-os_dbus-mqtt-solarsense/master/download.sh

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

If the script stops with the message
`dbus.exceptions.NameExistsException: Bus name already exists: com.victronenergy.meteo.mqtt_solarsense_40`
it means the service is still running or another service is using that bus name.

### Read settings

Values can be found on this MQTT path of the Venus OS broker:

```
N/<vrm_id>/meteo/40/...
```


## Tests

The Statestream → dbus translation logic lives in the dependency-free module
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
the Home Assistant MQTT Statestream → `com.victronenergy.meteo` parser.

Companion ESPHome decoder:
[tukutt/Victron-SolarSense-750-EspHome](https://github.com/tukutt/Victron-SolarSense-750-EspHome).
