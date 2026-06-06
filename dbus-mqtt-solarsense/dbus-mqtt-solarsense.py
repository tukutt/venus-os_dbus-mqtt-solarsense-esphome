#!/usr/bin/env python

from gi.repository import GLib  # pyright: ignore[reportMissingImports]
import platform
import logging
import sys
import os
from time import sleep, time
import configparser  # for config/ini file
import _thread

# import external packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext"))
import paho.mqtt.client as mqtt

# import Victron Energy packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))
from vedbus import VeDbusService  # noqa: E402
from ve_utils import get_vrm_portal_id  # noqa: E402

# import the (dependency-free) SolarSense / Statestream translation logic
from solarsense_parser import parse_message  # noqa: E402


# get values from config.ini file
try:
    config_file = (os.path.dirname(os.path.realpath(__file__))) + "/config.ini"
    if os.path.exists(config_file):
        config = configparser.ConfigParser()
        config.read(config_file)
        if config["MQTT"]["broker_address"] == "IP_ADDR_OR_FQDN":
            print('ERROR:The "config.ini" is using invalid default values like IP_ADDR_OR_FQDN. The driver restarts in 60 seconds.')
            sleep(60)
            sys.exit()
    else:
        print('ERROR:The "' + config_file + '" is not found. Did you copy or rename the "config.sample.ini" to "config.ini"? The driver restarts in 60 seconds.')
        sleep(60)
        sys.exit()

except Exception:
    exception_type, exception_object, exception_traceback = sys.exc_info()
    file = exception_traceback.tb_frame.f_code.co_filename
    line = exception_traceback.tb_lineno
    print(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
    print("ERROR:The driver restarts in 60 seconds.")
    sleep(60)
    sys.exit()


# Get logging level from config.ini
# ERROR = shows errors only
# WARNING = shows ERROR and warnings
# INFO = shows WARNING and running functions
# DEBUG = shows INFO and data/values
if "DEFAULT" in config and "logging" in config["DEFAULT"]:
    if config["DEFAULT"]["logging"] == "DEBUG":
        logging.basicConfig(level=logging.DEBUG)
    elif config["DEFAULT"]["logging"] == "INFO":
        logging.basicConfig(level=logging.INFO)
    elif config["DEFAULT"]["logging"] == "ERROR":
        logging.basicConfig(level=logging.ERROR)
    else:
        logging.basicConfig(level=logging.WARNING)
else:
    logging.basicConfig(level=logging.WARNING)


# ----- read driver specific settings from config.ini -----

def config_get(section, key, default):
    if section in config and key in config[section]:
        return config[section][key]
    return default


# watchdog timeout in seconds (0 = disabled). The SolarSense also reports at
# night (irradiance 0 is a real measurement), so the watchdog only ever fires on
# a genuine outage (ESP down / out of BLE range), not at sunset.
timeout = int(config_get("DEFAULT", "timeout", "300"))

# Home Assistant MQTT Statestream topic structure:
#   <base_topic>/sensor/<entity_id>/state
base_topic = config_get("MQTT", "base_topic", "statestream")

# Only entities whose entity_id starts with this prefix are bridged. The MQTT
# '+' wildcard cannot filter a partial level, so the prefix is filtered in
# Python (see solarsense_parser.parse_message).
entity_prefix = config_get("MQTT", "entity_prefix", "solarsense")


# ----- shared runtime state -----
mqtt_connected = 0
last_changed = 0
last_updated = 0


# formatting helpers (gettextcallback: p = path, v = value)
def _n(p, v):
    return str("%i" % v)


def _wm2(p, v):
    return str("%.1f" % v) + " W/m²"


def _w(p, v):
    return str("%i" % v) + " W"


def _kwh(p, v):
    return str("%.2f" % v) + " kWh"


def _degc(p, v):
    return str("%.1f" % v) + " °C"


def _volt(p, v):
    return str("%.2f" % v) + " V"


def _min(p, v):
    return str("%i" % v) + " min"


def _hex(p, v):
    try:
        return "0x%08X" % int(v)
    except (ValueError, TypeError):
        return str(v)


# dbus paths with their initial (idle) values and text formatters. The service
# registers immediately with these defaults (all 0) and is populated as
# Statestream messages arrive - no "minimum message" is required to start. HA
# Statestream publishes retained, so the full state is received right after the
# subscription.
#
# Float-typed paths are seeded with 0.0 so the dbus value keeps a float type.
meteo_dict = {
    "/Irradiance": {"value": 0.0, "textformat": _wm2},
    "/InstallationPower": {"value": 0, "textformat": _w},
    "/TodaysYield": {"value": 0.0, "textformat": _kwh},
    "/CellTemperature": {"value": 0.0, "textformat": _degc},
    "/BatteryVoltage": {"value": 0.0, "textformat": _volt},
    "/TimeSinceLastSun": {"value": 0, "textformat": _min},
    "/ErrorCode": {"value": 0, "textformat": _hex},
    "/Connected": {"value": 0, "textformat": _n},
}


"""
com.victronenergy.meteo -- paths used by this driver (official Victron paths)

/Irradiance          <-- current irradiance (W/m², float, 1 decimal)
/InstallationPower   <-- estimated installation power (W, int)
/TodaysYield         <-- yield since sunrise today (kWh, float, 2 decimals)
/CellTemperature     <-- internal panel temperature (°C, float, 1 decimal)
/BatteryVoltage      <-- sensor battery voltage (V, float, 2 decimals)
/TimeSinceLastSun    <-- minutes since sunset (int)
/ErrorCode           <-- warnings/alarms, bitwise (uint32)
/Connected           <-- 1 = receiving data, 0 = sensor offline / out of BLE range

The SolarSense does NOT provide /ExternalTemperature, /WindSpeed nor
/WindDirection, so those (otherwise valid meteo) paths are intentionally not
registered - publishing empty paths would show blank fields in the GUI.

NOTE: This driver is read/display only.
"""


# MQTT callbacks
def on_disconnect(client, userdata, flags, reason_code, properties):
    # Do NOT block here: paho's loop (started with loop_start and a configured
    # reconnect_delay_set) reconnects automatically in the background.
    global mqtt_connected
    mqtt_connected = 0
    if reason_code != 0:
        logging.warning("MQTT client: Unexpected disconnection (reason %s). paho will auto-reconnect." % str(reason_code))
    else:
        logging.info("MQTT client: Disconnected cleanly.")


def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected
    if reason_code == 0:
        logging.info("MQTT client: Connected to MQTT broker!")
        mqtt_connected = 1
        # Subscribe to every Statestream sensor; the entity_prefix is filtered in
        # Python because MQTT wildcards cannot match a partial topic level.
        subscribe_topic = "%s/sensor/+/state" % base_topic
        logging.info('MQTT client: Subscribing to "%s"' % subscribe_topic)
        client.subscribe(subscribe_topic)
    else:
        logging.error("MQTT client: Failed to connect, return code %d\n", reason_code)


def on_message(client, userdata, msg):
    try:
        global meteo_dict, last_changed

        payload = msg.payload.decode("utf-8") if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload)
        payload = payload.strip()

        result = parse_message(msg.topic, payload, base_topic, entity_prefix)
        if result is None:
            logging.debug('Ignored topic "%s" payload "%s"' % (msg.topic, payload))
            return

        path, value = result
        meteo_dict[path]["value"] = value

        # A valid measurement means the sensor is alive and in range.
        meteo_dict["/Connected"]["value"] = 1

        logging.debug('SolarSense %s = %s' % (path, value))
        last_changed = int(time())

    except Exception:
        exception_type, exception_object, exception_traceback = sys.exc_info()
        file = exception_traceback.tb_frame.f_code.co_filename
        line = exception_traceback.tb_lineno
        logging.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")
        logging.debug("MQTT topic: %s payload: %s" % (msg.topic, str(msg.payload)))


class DbusMqttSolarSenseService:
    def __init__(
        self,
        servicename,
        deviceinstance,
        paths,
        productname="SolarSense 750",
        customname="SolarSense 750",
        connection="SolarSense MQTT service",
    ):

        self._dbusservice = VeDbusService(servicename, register=False)
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion",
            "Unkown version, and running on Python " + platform.python_version(),
        )
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", productname)
        self._dbusservice.add_path("/CustomName", customname)
        self._dbusservice.add_path("/FirmwareVersion", "0.1.0 (20260606)")
        # self._dbusservice.add_path('/HardwareVersion', '')

        self._dbusservice.add_path("/Latency", None)

        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["value"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        # register VeDbusService after all paths where added
        self._dbusservice.register()

        GLib.timeout_add(1000, self._update)  # pause 1000ms before the next request

    def _update(self):

        global meteo_dict, last_changed, last_updated

        now = int(time())

        # Watchdog: if no Statestream message arrived for `timeout` seconds the
        # ESP32 is down or out of BLE range -> mark the sensor as not connected.
        # IMPORTANT: do NOT zero /Irradiance (or any measurement): an absence of
        # data is not a measured value of zero. The last known values are kept on
        # the bus and only /Connected is set to 0. (The SolarSense also reports at
        # night with irradiance 0, so this never fires merely at sunset.)
        if timeout != 0 and last_changed != 0 and (now - last_changed) > timeout:
            if meteo_dict["/Connected"]["value"] != 0:
                logging.warning("Watchdog: no SolarSense message for %i seconds, setting Connected=0 (sensor offline / out of BLE range). Measurements are kept." % (now - last_changed))
            meteo_dict["/Connected"]["value"] = 0
            last_changed = 0  # mark watchdog handled, avoid re-logging every second

        if last_changed != last_updated:

            for setting, data in meteo_dict.items():

                try:
                    self._dbusservice[setting] = data["value"]

                except TypeError as e:
                    logging.error('Received key "' + setting + '" with value "' + str(data["value"]) + '" is not valid: ' + str(e))

                except Exception:
                    exception_type, exception_object, exception_traceback = sys.exc_info()
                    file = exception_traceback.tb_frame.f_code.co_filename
                    line = exception_traceback.tb_lineno
                    logging.error(f"Exception occurred: {repr(exception_object)} of type {exception_type} in {file} line #{line}")

            logging.info("Data: {:.1f} W/m², connected {}".format(meteo_dict["/Irradiance"]["value"], meteo_dict["/Connected"]["value"]))

            last_updated = last_changed

        # increment UpdateIndex - to show that new data is available
        index = self._dbusservice["/UpdateIndex"] + 1  # increment index
        if index > 255:  # maximum value of the index
            index = 0  # overflow from 255 to 0
        self._dbusservice["/UpdateIndex"] = index
        return True

    def _handlechangedvalue(self, path, value):
        # Read/display only: the SolarSense (via HA) stays the master, we do not
        # push changes back.
        logging.debug("someone else updated %s to %s (ignored, driver is read-only)" % (path, value))
        return True  # accept the change locally so the UI stays responsive


def main():
    _thread.daemon = True  # allow the program to quit

    from dbus.mainloop.glib import (
        DBusGMainLoop,
    )  # pyright: ignore[reportMissingImports]

    # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
    DBusGMainLoop(set_as_default=True)

    # Register the dbus service FIRST, with idle defaults, before touching MQTT.
    # This way the sensor always appears in Venus OS / VRM even if the broker is
    # unreachable. It is then populated as Statestream messages arrive.
    paths_dbus = {
        "/UpdateIndex": {"value": 0, "textformat": _n},
    }
    paths_dbus.update(meteo_dict)

    DbusMqttSolarSenseService(
        servicename="com.victronenergy.meteo.mqtt_solarsense_" + str(config["DEFAULT"]["device_instance"]),
        deviceinstance=int(config["DEFAULT"]["device_instance"]),
        customname=config["DEFAULT"]["device_name"],
        paths=paths_dbus,
    )
    logging.info("Registered on dbus, setting up MQTT.")

    # MQTT setup
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="MqttSolarSense_" + get_vrm_portal_id() + "_" + str(config["DEFAULT"]["device_instance"]))
    client.on_disconnect = on_disconnect
    client.on_connect = on_connect
    client.on_message = on_message

    # check tls and use settings, if provided
    if "tls_enabled" in config["MQTT"] and config["MQTT"]["tls_enabled"] == "1":
        logging.info("MQTT client: TLS is enabled")

        if "tls_path_to_ca" in config["MQTT"] and config["MQTT"]["tls_path_to_ca"] != "":
            logging.info('MQTT client: TLS: custom ca "%s" used' % config["MQTT"]["tls_path_to_ca"])
            client.tls_set(config["MQTT"]["tls_path_to_ca"], tls_version=2)
        else:
            client.tls_set(tls_version=2)

        if "tls_insecure" in config["MQTT"] and config["MQTT"]["tls_insecure"] != "":
            logging.info("MQTT client: TLS certificate server hostname verification disabled")
            client.tls_insecure_set(True)

    # check if username and password are set
    if "username" in config["MQTT"] and "password" in config["MQTT"] and config["MQTT"]["username"] != "" and config["MQTT"]["password"] != "":
        logging.info('MQTT client: Using username "%s" and password to connect' % config["MQTT"]["username"])
        client.username_pw_set(username=config["MQTT"]["username"], password=config["MQTT"]["password"])

    # Connect in the background and let paho's loop handle (re)connection on its
    # own thread. connect_async never blocks; reconnect_delay_set bounds the
    # automatic retry backoff. on_connect (re)subscribes on every (re)connection.
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    logging.info(f"MQTT client: Connecting to broker {config['MQTT']['broker_address']} on port {config['MQTT']['broker_port']}")
    client.connect_async(host=config["MQTT"]["broker_address"], port=int(config["MQTT"]["broker_port"]))
    client.loop_start()

    logging.info("Switching over to GLib.MainLoop() (= event based)")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
