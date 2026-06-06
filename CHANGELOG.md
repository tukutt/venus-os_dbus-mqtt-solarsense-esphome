# Changelog

## 0.1.0
* Forked from `dbus-mqtt-evcc` as `dbus-mqtt-solarsense`
* Changed: target the native `com.victronenergy.meteo` service instead of
  `com.victronenergy.evcharger` (paths `/Irradiance`, `/InstallationPower`,
  `/TodaysYield`, `/CellTemperature`, `/BatteryVoltage`, `/TimeSinceLastSun`,
  `/ErrorCode`, `/Connected`)
* Added: Home Assistant MQTT Statestream parser (`solarsense_parser.py`),
  subscribing to `<base_topic>/sensor/+/state` and mapping entity_ids to dbus
  paths by **suffix** (robust to prefix duplication and the "Today's Yield"
  apostrophe slug)
* Added: `base_topic` and `entity_prefix` config settings; the entity prefix is
  filtered in Python since MQTT wildcards cannot match a partial topic level
* Added: ErrorCode parsing from hex (`0x04001405`) or decimal to a native uint32
* Changed: watchdog sets `/Connected = 0` on sensor silence but **keeps** the
  last measurements (an absence of data is not a measured zero); it never fires
  merely at night (irradiance 0 is a real measurement)
* Excluded: `*charger_error` and the `Low Battery` binary sensor are not bridged
* Kept: dbus registration via `vedbus`/`VeDbusService`, VRM instance handling,
  daemon-tools service structure, install/uninstall/restart/download scripts,
  non-blocking MQTT (`connect_async` + `reconnect_delay_set`), service registers
  immediately with idle defaults (no "minimum message")
* Added: unit tests for the Statestream → dbus translation (`test/test_parser.py`)
* Note: read/display only
