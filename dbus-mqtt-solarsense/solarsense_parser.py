#!/usr/bin/env python

"""
ESPHome native MQTT -> Victron dbus (com.victronenergy.meteo) translation logic
for the Victron SolarSense 750 irradiance sensor.

This module is intentionally free of any external dependency (no GLib, no dbus,
no paho). It only contains pure functions and small constants so the logic can
be unit-tested off-device (see test/test_parser.py).

Data path:

    SolarSense 750 (BLE)
      -> ESP32 running the ESPHome decoder, with the native ESPHome `mqtt:`
         component enabled (the ESP publishes directly to the broker; Home
         Assistant is NOT in the loop)
      -> MQTT broker
      -> this driver (on the Cerbo GX)
      -> com.victronenergy.meteo on the dbus
      -> Remote Console / VRM

ESPHome publishes one retained topic per entity, using the platform as the
second topic level:

    <base_topic>/sensor/<object_id>/state          payload = value string
    <base_topic>/text_sensor/<object_id>/state     payload = free string
    <base_topic>/binary_sensor/<object_id>/state   payload = "ON"/"OFF"

plus a Last-Will availability topic:

    <base_topic>/status                            payload = "online"/"offline"

`<base_topic>` defaults to the ESPHome node name (e.g. "esphome-ble-proxy").
`<object_id>` is the slugified sensor name, e.g. "solarsense_irradiance".

Examples:

    esphome-ble-proxy/sensor/solarsense_irradiance/state       -> "337.3"
    esphome-ble-proxy/sensor/solarsense_today_s_yield/state    -> "0.07"
    esphome-ble-proxy/text_sensor/solarsense_error_code/state  -> "0x04001405"

IMPORTANT: the SolarSense "Error Code" / "Charger Error" are ESPHome
text_sensors, so they live under .../text_sensor/... (not .../sensor/...). This
is why the driver subscribes to <base_topic>/+/+/state (any platform level), not
just .../sensor/....

Because an ESPHome device name can duplicate the sensor prefix (giving
``solarsense_solarsense_irradiance``) and because the apostrophe in "Today's
Yield" slugifies unpredictably (``today_s_yield`` / ``todays_yield`` / ...), the
entity -> dbus mapping matches on the *suffix* of the object_id, never on an
exact string. Hyphens and underscores are treated as equivalent so the mapping
also tolerates ESPHome versions that slugify with '-' instead of '_'.
"""


# ---------------------------------------------------------------------------
# Invalid / "no value" payloads
# ---------------------------------------------------------------------------
# Placeholder states published when an entity has no usable value (these come
# mostly from Home Assistant, but are accepted here too for robustness). They
# must be ignored so a glitch never overwrites the last known good value.
INVALID_VALUES = ("", "unavailable", "unknown", "nan", "none", "null", "-")

# Availability (Last-Will) payloads on <base_topic>/status
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"


# ---------------------------------------------------------------------------
# object_id suffix -> dbus path mapping (ORDER MATTERS, first match wins)
# ---------------------------------------------------------------------------
# Edit this table to add/rename sensors. Each entry is:
#   (object_id suffix, dbus path, value kind)
#
# Suffixes are written with underscores; matching is done after normalising
# both sides (lower-case, '-' -> '_'), so '-'-slugified ids match too.
#
# value kinds:
#   "float" -> parsed as float, kept as a native float
#   "int"   -> parsed as float then rounded to a native int
#   "hex"   -> "0x04001405" (or a decimal string) parsed to a native int
#
# IMPORTANT ordering / exclusion notes:
#   * ``charger_error`` is NOT bridged to dbus and must be excluded BEFORE the
#     ``error_code`` rule is ever considered (see EXCLUDE_SUFFIXES).
#   * ``error_code`` is listed first so it is matched before any (future) more
#     generic rule could shadow it.
ENTITY_MAP = (
    ("error_code", "/ErrorCode", "hex"),
    ("irradiance", "/Irradiance", "float"),
    ("installation_power", "/InstallationPower", "int"),
    ("yield", "/TodaysYield", "float"),
    ("cell_temperature", "/CellTemperature", "float"),
    ("battery_voltage", "/BatteryVoltage", "float"),
    ("time_since_last_sun", "/TimeSinceLastSun", "int"),
)

# object_id suffixes that are explicitly NOT bridged to dbus. Checked before
# ENTITY_MAP so e.g. ``*charger_error`` is dropped and never confused with the
# ``*error_code`` rule.
EXCLUDE_SUFFIXES = ("charger_error",)


def _normalise(object_id):
    """Lower-case and treat '-' and '_' as equivalent for suffix matching."""
    return object_id.lower().replace("-", "_")


def _is_invalid(raw):
    return raw is None or str(raw).strip().lower() in INVALID_VALUES


def parse_float(raw):
    """Return raw as float, or None if it is missing/invalid."""
    if _is_invalid(raw):
        return None
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError):
        return None


def parse_int(raw):
    """Return raw rounded to a native int, or None if it is missing/invalid."""
    value = parse_float(raw)
    if value is None:
        return None
    return int(round(value))


def parse_error_code(raw):
    """
    Parse a SolarSense error/warning code into a native int (uint32 bitwise).

    Accepts the hex string the ESPHome decoder publishes ("0x04001405") as well
    as a plain decimal string. Returns None for missing/invalid payloads.
    """
    if _is_invalid(raw):
        return None
    s = str(raw).strip()
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s, 10)
    except (ValueError, TypeError):
        return None


# kind -> parser function
_PARSERS = {
    "float": parse_float,
    "int": parse_int,
    "hex": parse_error_code,
}


def object_id_from_topic(topic, base_topic=""):
    """
    Return the object_id for an ESPHome state topic of the shape
    ``<base_topic>/<platform>/<object_id>/state`` (any platform level: sensor,
    text_sensor, binary_sensor, ...), or None if the topic does not match.

    If ``base_topic`` is empty, the base is not checked (wildcard mode): the
    object_id is taken as the second-to-last topic level. If ``base_topic`` is
    given, the leading levels must equal it exactly.

    Examples:
      ("esphome-ble-proxy/sensor/solarsense_irradiance/state", "esphome-ble-proxy")
          -> "solarsense_irradiance"
      ("esphome-ble-proxy/text_sensor/solarsense_error_code/state", "")
          -> "solarsense_error_code"
    """
    parts = topic.split("/")
    # need at least <platform>/<object_id>/state, i.e. base + 3 levels
    if len(parts) < 4 or parts[-1] != "state":
        return None
    object_id = parts[-2]
    if not object_id:
        return None
    if base_topic:
        base = "/".join(parts[:-3])
        if base != base_topic:
            return None
    return object_id


def match_path(object_id):
    """
    Return (dbus_path, kind) for an object_id by matching on its suffix, or None
    if the entity is unknown or explicitly excluded.
    """
    oid = _normalise(object_id)
    for excluded in EXCLUDE_SUFFIXES:
        if oid.endswith(excluded):
            return None
    for suffix, path, kind in ENTITY_MAP:
        if oid.endswith(suffix):
            return path, kind
    return None


def parse_value(kind, raw):
    """Parse a raw payload according to its kind. Returns a native value or None."""
    parser = _PARSERS.get(kind)
    if parser is None:
        return None
    return parser(raw)


def parse_status(payload):
    """
    Parse a <base_topic>/status availability payload.

    Returns True for "online", False for "offline", or None if unrecognised.
    """
    if payload is None:
        return None
    s = str(payload).strip().lower()
    if s == STATUS_ONLINE:
        return True
    if s == STATUS_OFFLINE:
        return False
    return None


def parse_message(topic, payload, base_topic="", entity_prefix="solarsense"):
    """
    Full ESPHome state-topic pipeline.

    Given a raw MQTT (topic, payload), return (dbus_path, native_value) if the
    message is a known, in-prefix SolarSense sensor with a valid value, otherwise
    None.

    Filtering order:
      1. topic must match <base_topic>/<platform>/<object_id>/state
         (base_topic empty = accept any base, "wildcard" mode)
      2. object_id must start with entity_prefix (the MQTT '+' wildcard cannot
         filter a partial level, so we filter here)
      3. object_id must map to a dbus path (and not be excluded, e.g. charger_error)
      4. payload must parse to a valid value (invalid/unavailable -> None)
    """
    object_id = object_id_from_topic(topic, base_topic)
    if object_id is None:
        return None

    if entity_prefix and not _normalise(object_id).startswith(_normalise(entity_prefix)):
        return None

    matched = match_path(object_id)
    if matched is None:
        return None
    path, kind = matched

    value = parse_value(kind, payload)
    if value is None:
        return None

    return path, value
