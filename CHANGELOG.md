# Changelog

## 0.1.0
* Forked from `dbus-mqtt-evcc` as `dbus-mqtt-solarsense`
* Changed: target the native `com.victronenergy.meteo` service instead of
  `com.victronenergy.evcharger` (paths `/Irradiance`, `/InstallationPower`,
  `/TodaysYield`, `/CellTemperature`, `/BatteryVoltage`, `/TimeSinceLastSun`,
  `/ErrorCode`, `/Connected`)
* Added: ESPHome native MQTT parser (`solarsense_parser.py`). The ESP32 runs the
  ESPHome `mqtt:` component and publishes directly to the broker; Home Assistant
  is not in the path.
* Added: subscribe to `<base_topic>/+/+/state` so **both** `sensor/` and
  `text_sensor/` platform levels are received (the SolarSense *Error Code* is an
  ESPHome text_sensor)
* Added: `base_topic` (optional, = ESPHome node name; empty = broker-wide
  wildcard matched on `entity_prefix` only) and `entity_prefix` settings; the
  prefix is filtered in Python since MQTT wildcards cannot match a partial level
* Added: object_id → dbus path mapping by **suffix**, tolerant to prefix
  duplication, the "Today's Yield" apostrophe slug, and `-`/`_` slug variants
* Added: `/Connected` driven by the ESPHome `<base_topic>/status` availability
  LWT (`online`/`offline`) when `base_topic` is set
* Added: ErrorCode parsing from hex (`0x04001405`) or decimal to a native uint32
* Changed: watchdog set `/Connected = 0` on data silence but **keeps** the last
  measurements (an absence of data is not a measured zero). Only real sensor
  messages reset the watchdog (not `/status`), so an out-of-BLE-range sensor is
  caught even while the ESP reports itself online. Never fires merely at night.
* Excluded: `*charger_error` and the `low_battery` binary sensor are not bridged
* Kept: dbus registration via `vedbus`/`VeDbusService`, VRM instance handling,
  daemon-tools service structure, install/uninstall/restart/download scripts,
  non-blocking MQTT (`connect_async` + `reconnect_delay_set`), service registers
  immediately with idle defaults (no "minimum message")
* Added: unit tests for the ESPHome → dbus translation (`test/test_parser.py`)
* Note: read/display only
