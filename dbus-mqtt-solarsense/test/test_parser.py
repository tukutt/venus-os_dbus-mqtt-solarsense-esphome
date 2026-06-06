#!/usr/bin/env python3

"""
Unit tests for the ESPHome native MQTT -> Victron dbus
(com.victronenergy.meteo) translation logic for the SolarSense 750.

These tests simulate ESPHome state-topic messages (topic/payload pairs),
including the awkward real-world cases:
  * topics on different platform levels: sensor/ AND text_sensor/
  * "unavailable" / "unknown" / "nan" / "" payloads
  * ErrorCode as a hex string "0x04001405"
  * Today's Yield slug ("today_s_yield") -> "0.07"
  * time_since_last_sun "330"
  * an object_id that duplicates the prefix (solarsense_solarsense_irradiance)
  * a *charger_error entity that must be IGNORED (not bridged to dbus)
  * '-'-slugified ids (some ESPHome versions) treated like '_'
  * wildcard mode (empty base_topic) vs. an explicit base_topic
  * the <base>/status availability LWT

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
    parse_status,
    object_id_from_topic,
    match_path,
    parse_message,
)

NODE = "esphome-ble-proxy"


def sensor_topic(object_id, base=NODE):
    return "%s/sensor/%s/state" % (base, object_id)


def text_topic(object_id, base=NODE):
    return "%s/text_sensor/%s/state" % (base, object_id)


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

    def test_parse_status(self):
        self.assertTrue(parse_status("online"))
        self.assertFalse(parse_status("offline"))
        self.assertIsNone(parse_status("whatever"))
        self.assertIsNone(parse_status(None))


class TestTopicParsing(unittest.TestCase):
    def test_object_id_from_sensor_topic(self):
        self.assertEqual(object_id_from_topic(sensor_topic("solarsense_irradiance"), NODE), "solarsense_irradiance")

    def test_object_id_from_text_sensor_topic(self):
        # error_code is a text_sensor -> different platform level, still parsed
        self.assertEqual(object_id_from_topic(text_topic("solarsense_error_code"), NODE), "solarsense_error_code")

    def test_wrong_base_topic_ignored(self):
        self.assertIsNone(object_id_from_topic(sensor_topic("solarsense_irradiance", base="other-node"), NODE))

    def test_wildcard_base_accepts_any_node(self):
        self.assertEqual(object_id_from_topic(sensor_topic("solarsense_irradiance", base="anything"), ""), "solarsense_irradiance")

    def test_non_state_topic_ignored(self):
        self.assertIsNone(object_id_from_topic("%s/sensor/solarsense_irradiance/config" % NODE, NODE))

    def test_status_topic_is_not_a_state_topic(self):
        self.assertIsNone(object_id_from_topic("%s/status" % NODE, NODE))


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
        for oid, path in cases.items():
            matched = match_path(oid)
            self.assertIsNotNone(matched, "no match for %s" % oid)
            self.assertEqual(matched[0], path)

    def test_hyphen_slug_matches_like_underscore(self):
        self.assertEqual(match_path("solarsense-installation-power")[0], "/InstallationPower")

    def test_prefix_duplicate_still_matches_on_suffix(self):
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
        self.assertEqual(parse_message(sensor_topic("solarsense_irradiance"), "337.3", NODE), ("/Irradiance", 337.3))

    def test_installation_power_is_int(self):
        path, value = parse_message(sensor_topic("solarsense_installation_power"), "412.0", NODE)
        self.assertEqual(path, "/InstallationPower")
        self.assertEqual(value, 412)
        self.assertIsInstance(value, int)

    def test_todays_yield(self):
        self.assertEqual(parse_message(sensor_topic("solarsense_today_s_yield"), "0.07", NODE), ("/TodaysYield", 0.07))

    def test_time_since_last_sun(self):
        self.assertEqual(parse_message(sensor_topic("solarsense_time_since_last_sun"), "330", NODE), ("/TimeSinceLastSun", 330))

    def test_error_code_hex_on_text_sensor_level(self):
        # error_code arrives under text_sensor/, not sensor/
        path, value = parse_message(text_topic("solarsense_error_code"), "0x04001405", NODE)
        self.assertEqual(path, "/ErrorCode")
        self.assertEqual(value, 0x04001405)
        self.assertIsInstance(value, int)

    def test_prefix_duplicate_entity(self):
        self.assertEqual(parse_message(sensor_topic("solarsense_solarsense_irradiance"), "500.5", NODE), ("/Irradiance", 500.5))

    def test_charger_error_ignored(self):
        self.assertIsNone(parse_message(text_topic("solarsense_charger_error"), "0x00000000", NODE))

    def test_unavailable_ignored(self):
        self.assertIsNone(parse_message(sensor_topic("solarsense_irradiance"), "unavailable", NODE))
        self.assertIsNone(parse_message(sensor_topic("solarsense_cell_temperature"), "nan", NODE))
        self.assertIsNone(parse_message(sensor_topic("solarsense_battery_voltage"), "", NODE))

    def test_other_prefix_ignored(self):
        # the ESP's own system sensors (e.g. wifi/uptime) are dropped
        self.assertIsNone(parse_message(sensor_topic("uptime"), "123", NODE))
        self.assertIsNone(parse_message(sensor_topic("wifi_signal_db"), "-52", NODE))

    def test_wildcard_mode_no_base(self):
        # empty base_topic: match any node, filter on object_id prefix only
        self.assertEqual(parse_message(sensor_topic("solarsense_irradiance", base="whatever"), "200.0", ""), ("/Irradiance", 200.0))


class TestNativeTypes(unittest.TestCase):
    def test_no_strings_on_measurement_paths(self):
        samples = [
            (sensor_topic("solarsense_irradiance"), "337.3"),
            (sensor_topic("solarsense_installation_power"), "412"),
            (sensor_topic("solarsense_today_s_yield"), "0.07"),
            (sensor_topic("solarsense_cell_temperature"), "21.4"),
            (sensor_topic("solarsense_battery_voltage"), "3.28"),
            (sensor_topic("solarsense_time_since_last_sun"), "330"),
            (text_topic("solarsense_error_code"), "0x04001405"),
        ]
        for t, p in samples:
            result = parse_message(t, p, NODE)
            self.assertIsNotNone(result, "no result for %s = %s" % (t, p))
            _, value = result
            self.assertNotIsInstance(value, str, "%s should not be a string" % t)


class TestRealisticStream(unittest.TestCase):
    def test_full_message_stream(self):
        """Replay a full retained ESPHome burst as received right after subscribe."""
        stream = [
            (sensor_topic("solarsense_irradiance"), "337.3"),
            (sensor_topic("solarsense_installation_power"), "412"),
            (sensor_topic("solarsense_today_s_yield"), "0.07"),
            (sensor_topic("solarsense_cell_temperature"), "21.4"),
            (sensor_topic("solarsense_battery_voltage"), "3.28"),
            (sensor_topic("solarsense_time_since_last_sun"), "0"),
            (text_topic("solarsense_error_code"), "0x04001405"),
            (text_topic("solarsense_charger_error"), "0x00000000"),    # must be ignored
            ("%s/binary_sensor/low_battery/state" % NODE, "OFF"),       # not in prefix/map
            (sensor_topic("uptime"), "98712"),                          # ESP system sensor
            (sensor_topic("solarsense_irradiance"), "unavailable"),     # glitch, must be ignored
        ]
        produced = {}
        for t, p in stream:
            result = parse_message(t, p, NODE)
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
        # charger_error, low_battery and uptime never produced a dbus path
        self.assertEqual(len(produced), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
