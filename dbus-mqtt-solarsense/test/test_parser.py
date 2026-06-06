#!/usr/bin/env python3

"""
Unit tests for the Home Assistant MQTT Statestream -> Victron dbus
(com.victronenergy.meteo) translation logic for the SolarSense 750.

These tests simulate Statestream MQTT messages (topic/payload pairs), including
the awkward real-world cases:
  * "unavailable" / "unknown" / "nan" / "" payloads (ESP offline / no value)
  * ErrorCode as a hex string "0x04001405"
  * Today's Yield slug ("today_s_yield") -> "0.07"
  * time_since_last_sun "330"
  * an entity_id that duplicates the prefix (solarsense_solarsense_irradiance)
  * a *charger_error entity that must be IGNORED (not bridged to dbus)

Run with:  python3 test/test_parser.py
(no external dependencies, no dbus/GLib/paho needed)
"""

import os
import sys
import unittest

# make the driver's solarsense_parser importable when run from anywhere
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from solarsense_parser import (  # noqa: E402
    parse_float,
    parse_int,
    parse_error_code,
    entity_id_from_topic,
    match_path,
    parse_message,
)


def topic(entity_id, base="statestream"):
    return "%s/sensor/%s/state" % (base, entity_id)


class TestValueParsing(unittest.TestCase):
    def test_parse_float_valid(self):
        self.assertEqual(parse_float("337.3"), 337.3)
        self.assertEqual(parse_float("0.07"), 0.07)
        self.assertEqual(parse_float(" 16 "), 16.0)

    def test_parse_float_invalid(self):
        for bad in ("unavailable", "unknown", "nan", "NaN", "", "none", "null", "-", None, "abc"):
            self.assertIsNone(parse_float(bad), "expected None for %r" % bad)

    def test_parse_int_rounds(self):
        self.assertEqual(parse_int("330"), 330)
        self.assertEqual(parse_int("412.7"), 413)
        self.assertIsNone(parse_int("unavailable"))

    def test_parse_error_code_hex(self):
        self.assertEqual(parse_error_code("0x04001405"), 0x04001405)
        self.assertEqual(parse_error_code("0x00000000"), 0)

    def test_parse_error_code_decimal(self):
        self.assertEqual(parse_error_code("67113477"), 67113477)  # = 0x04001405

    def test_parse_error_code_invalid(self):
        for bad in ("unavailable", "unknown", "", "nan", None, "zzz"):
            self.assertIsNone(parse_error_code(bad), "expected None for %r" % bad)


class TestTopicParsing(unittest.TestCase):
    def test_entity_id_extracted(self):
        self.assertEqual(entity_id_from_topic("statestream/sensor/solarsense_irradiance/state", "statestream"), "solarsense_irradiance")

    def test_wrong_base_topic_ignored(self):
        self.assertIsNone(entity_id_from_topic("homeassistant/sensor/solarsense_irradiance/state", "statestream"))

    def test_non_state_topic_ignored(self):
        self.assertIsNone(entity_id_from_topic("statestream/sensor/solarsense_irradiance/attributes", "statestream"))

    def test_binary_sensor_topic_ignored(self):
        # we only subscribe to .../sensor/...; binary_sensor is not bridged
        self.assertIsNone(entity_id_from_topic("statestream/binary_sensor/low_battery/state", "statestream"))


class TestMatchPath(unittest.TestCase):
    def test_each_known_suffix(self):
        cases = {
            "solarsense_irradiance": "/Irradiance",
            "solarsense_installation_power": "/InstallationPower",
            "solarsense_today_s_yield": "/TodaysYield",
            "solarsense_todays_yield": "/TodaysYield",
            "solarsense_cell_temperature": "/CellTemperature",
            "solarsense_battery_voltage": "/BatteryVoltage",
            "solarsense_time_since_last_sun": "/TimeSinceLastSun",
            "solarsense_error_code": "/ErrorCode",
        }
        for eid, path in cases.items():
            matched = match_path(eid)
            self.assertIsNotNone(matched, "no match for %s" % eid)
            self.assertEqual(matched[0], path)

    def test_prefix_duplicate_still_matches_on_suffix(self):
        # ESPHome device name duplicating the prefix must not break matching
        matched = match_path("solarsense_solarsense_irradiance")
        self.assertIsNotNone(matched)
        self.assertEqual(matched[0], "/Irradiance")

    def test_charger_error_excluded(self):
        # charger_error must NOT be bridged, and must not be confused with error_code
        self.assertIsNone(match_path("solarsense_charger_error"))

    def test_unknown_entity_ignored(self):
        self.assertIsNone(match_path("solarsense_wifi_signal"))


class TestParseMessage(unittest.TestCase):
    def test_irradiance(self):
        self.assertEqual(parse_message(topic("solarsense_irradiance"), "337.3"), ("/Irradiance", 337.3))

    def test_installation_power_is_int(self):
        path, value = parse_message(topic("solarsense_installation_power"), "412.0")
        self.assertEqual(path, "/InstallationPower")
        self.assertEqual(value, 412)
        self.assertIsInstance(value, int)

    def test_todays_yield(self):
        self.assertEqual(parse_message(topic("solarsense_today_s_yield"), "0.07"), ("/TodaysYield", 0.07))

    def test_time_since_last_sun(self):
        self.assertEqual(parse_message(topic("solarsense_time_since_last_sun"), "330"), ("/TimeSinceLastSun", 330))

    def test_error_code_hex(self):
        path, value = parse_message(topic("solarsense_error_code"), "0x04001405")
        self.assertEqual(path, "/ErrorCode")
        self.assertEqual(value, 0x04001405)
        self.assertIsInstance(value, int)

    def test_prefix_duplicate_entity(self):
        self.assertEqual(parse_message(topic("solarsense_solarsense_irradiance"), "500.5"), ("/Irradiance", 500.5))

    def test_charger_error_ignored(self):
        self.assertIsNone(parse_message(topic("solarsense_charger_error"), "0x00000000"))

    def test_unavailable_ignored(self):
        self.assertIsNone(parse_message(topic("solarsense_irradiance"), "unavailable"))
        self.assertIsNone(parse_message(topic("solarsense_cell_temperature"), "nan"))
        self.assertIsNone(parse_message(topic("solarsense_battery_voltage"), ""))

    def test_wrong_prefix_ignored(self):
        # entity outside the configured prefix is dropped (Python-side filter)
        self.assertIsNone(parse_message(topic("washingmachine_power"), "123"))

    def test_custom_base_topic_and_prefix(self):
        msg = parse_message("ha/sensor/ss_irradiance/state", "200.0", base_topic="ha", entity_prefix="ss")
        self.assertEqual(msg, ("/Irradiance", 200.0))


class TestNativeTypes(unittest.TestCase):
    def test_no_strings_on_measurement_paths(self):
        samples = [
            (topic("solarsense_irradiance"), "337.3"),
            (topic("solarsense_installation_power"), "412"),
            (topic("solarsense_today_s_yield"), "0.07"),
            (topic("solarsense_cell_temperature"), "21.4"),
            (topic("solarsense_battery_voltage"), "3.28"),
            (topic("solarsense_time_since_last_sun"), "330"),
            (topic("solarsense_error_code"), "0x04001405"),
        ]
        for t, p in samples:
            result = parse_message(t, p)
            self.assertIsNotNone(result, "no result for %s = %s" % (t, p))
            _, value = result
            self.assertNotIsInstance(value, str, "%s should not be a string" % t)


class TestRealisticStream(unittest.TestCase):
    def test_full_message_stream(self):
        """Replay a full retained Statestream burst as received right after subscribe."""
        stream = [
            (topic("solarsense_irradiance"), "337.3"),
            (topic("solarsense_installation_power"), "412"),
            (topic("solarsense_today_s_yield"), "0.07"),
            (topic("solarsense_cell_temperature"), "21.4"),
            (topic("solarsense_battery_voltage"), "3.28"),
            (topic("solarsense_time_since_last_sun"), "0"),
            (topic("solarsense_error_code"), "0x04001405"),
            (topic("solarsense_charger_error"), "0x00000000"),   # must be ignored
            ("statestream/binary_sensor/low_battery/state", "off"),  # not subscribed/bridged
            (topic("solarsense_irradiance"), "unavailable"),     # glitch, must be ignored
        ]
        produced = {}
        for t, p in stream:
            result = parse_message(t, p)
            if result is not None:
                path, value = result
                produced[path] = value

        self.assertEqual(produced["/Irradiance"], 337.3)        # not wiped by "unavailable"
        self.assertEqual(produced["/InstallationPower"], 412)
        self.assertEqual(produced["/TodaysYield"], 0.07)
        self.assertEqual(produced["/CellTemperature"], 21.4)
        self.assertEqual(produced["/BatteryVoltage"], 3.28)
        self.assertEqual(produced["/TimeSinceLastSun"], 0)
        self.assertEqual(produced["/ErrorCode"], 0x04001405)
        # charger_error and low_battery never produced a dbus path
        self.assertNotIn("/ChargerError", produced)
        self.assertEqual(len(produced), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
