#!/usr/bin/env python

"""
Home Assistant MQTT Statestream -> Victron dbus (com.victronenergy.meteo)
translation logic for the Victron SolarSense 750 irradiance sensor.

This module is intentionally free of any external dependency (no GLib, no dbus,
no paho). It only contains pure functions and small constants so the logic can
be unit-tested off-device (see test/test_parser.py).

Data path:

    SolarSense 750 (BLE)
      -> ESP32 running the ESPHome decoder (native HA API, NOT mqtt:)
      -> Home Assistant
      -> HA "MQTT Statestream" integration republishes each entity as:

            <base_topic>/sensor/<entity_id>/state          payload = value string
            <base_topic>/binary_sensor/<entity_id>/state   payload = "on"/"off"

      -> this driver (subscribes to <base_topic>/sensor/+/state on the HA broker)
      -> com.victronenergy.meteo on the Cerbo GX dbus
      -> Remote Console / VRM

Example topic / payload:

    statestream/sensor/solarsense_irradiance/state       -> "337.3"
    statestream/sensor/solarsense_today_s_yield/state     -> "0.07"
    statestream/sensor/solarsense_error_code/state        -> "0x04001405"

The ESPHome sensors are named "SolarSense <something>", so the HA entity_ids are
slugified to e.g. ``solarsense_irradiance``. Because an ESPHome device name can
duplicate the sensor prefix (giving ``solarsense_solarsense_irradiance``) and
because the apostrophe in "Today's Yield" slugifies unpredictably
(``today_s_yield`` / ``todays_yield`` / ...), the entity -> dbus mapping matches
on the *suffix* of the entity_id, never on an exact string.
"""


# ---------------------------------------------------------------------------
# Invalid / "no value" payloads
# ---------------------------------------------------------------------------
# Home Assistant publishes these placeholder states when the source entity has
# no usable value (e.g. the ESP32 is offline or out of BLE range). They must be
# ignored so a glitch never overwrites the last known good value on the bus.
INVALID_VALUES = ("", "unavailable", "unknown", "nan", "none", "null", "-")


# ---------------------------------------------------------------------------
# entity_id suffix -> dbus path mapping (ORDER MATTERS, first match wins)
# ---------------------------------------------------------------------------
# Edit this table to add/rename sensors. Each entry is:
#   (entity_id suffix, dbus path, value kind)
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

# entity_id suffixes that are explicitly NOT bridged to dbus. Checked before
# ENTITY_MAP so e.g. ``*charger_error`` is dropped and never confused with the
# ``*error_code`` rule.
EXCLUDE_SUFFIXES = ("charger_error",)


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


def entity_id_from_topic(topic, base_topic):
    """
    Return the entity_id for a Statestream ``sensor`` state topic, or None if the
    topic does not match ``<base_topic>/sensor/<entity_id>/state``.

    Example: ("statestream/sensor/solarsense_irradiance/state", "statestream")
             -> "solarsense_irradiance"
    """
    prefix = "%s/sensor/" % base_topic
    suffix = "/state"
    if not topic.startswith(prefix) or not topic.endswith(suffix):
        return None
    entity_id = topic[len(prefix):-len(suffix)]
    # an entity_id is a single level; reject anything with an extra slash
    if not entity_id or "/" in entity_id:
        return None
    return entity_id


def match_path(entity_id):
    """
    Return (dbus_path, kind) for an entity_id by matching on its suffix, or None
    if the entity is unknown or explicitly excluded.
    """
    eid = entity_id.lower()
    for excluded in EXCLUDE_SUFFIXES:
        if eid.endswith(excluded):
            return None
    for suffix, path, kind in ENTITY_MAP:
        if eid.endswith(suffix):
            return path, kind
    return None


def parse_value(kind, raw):
    """Parse a raw payload according to its kind. Returns a native value or None."""
    parser = _PARSERS.get(kind)
    if parser is None:
        return None
    return parser(raw)


def parse_message(topic, payload, base_topic="statestream", entity_prefix="solarsense"):
    """
    Full Statestream message pipeline.

    Given a raw MQTT (topic, payload), return (dbus_path, native_value) if the
    message is a known, in-prefix SolarSense sensor with a valid value, otherwise
    None.

    Filtering order:
      1. topic must match <base_topic>/sensor/<entity_id>/state
      2. entity_id must start with entity_prefix (the MQTT '+' wildcard cannot
         filter a partial level, so we filter here)
      3. entity_id must map to a dbus path (and not be excluded, e.g. charger_error)
      4. payload must parse to a valid value (invalid/unavailable -> None)
    """
    entity_id = entity_id_from_topic(topic, base_topic)
    if entity_id is None:
        return None

    if entity_prefix and not entity_id.lower().startswith(entity_prefix.lower()):
        return None

    matched = match_path(entity_id)
    if matched is None:
        return None
    path, kind = matched

    value = parse_value(kind, payload)
    if value is None:
        return None

    return path, value
