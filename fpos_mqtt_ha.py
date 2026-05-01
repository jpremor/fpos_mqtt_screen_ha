import paho.mqtt.client as mqtt
import json
import time
import ssl
import os
from dotenv import load_dotenv
import subprocess
import threading
from evdev import InputDevice, ecodes
from percent_to_raw import *
from dotenv import set_key
import socket

# Load environment variables
load_dotenv()

BROKER = os.getenv("BROKER_IP")
PORT = int(os.getenv("BROKER_PORT"))
USERNAME = os.getenv("BROKER_USERNAME")
PASSWORD = os.getenv("BROKER_PASSWORD")
CA_CERT = "/tmp-remorh/ca.crt"
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "10-0045")
DIMMING_PERCENT = int(os.getenv("DIMMING_PERCENT", "20"))

# Dynamically find the touch device event number
def find_touch_device():
    display_device_name = os.getenv("DISPLAY_DEVICE_NAME", "ft5x06")
    try:
        with open("/proc/bus/input/devices", "r") as f:
            lines = f.readlines()
        event_num = None
        found = False
        for i, line in enumerate(lines):
            if f'Name="' in line and display_device_name in line:
                found = True
            if found and 'Handlers=' in line:
                parts = line.split()
                for part in parts:
                    if part.startswith('event'):
                        event_num = part
                        break
                if event_num:
                    break
        if event_num:
            return f"/dev/input/{event_num}"
    except Exception as e:
        print(f"Error finding touch device: {e}")
    # fallback
    return os.getenv("TOUCH_DEVICE", "/dev/input/event5")

TOUCH_DEVICE = find_touch_device()
print(f"Found touch device: {TOUCH_DEVICE}")

# Track MQTT connection status
mqtt_connected = False

# Get hostname
HOSTNAME = socket.gethostname()

# Home Assistant friendly identifiers
DEVICE_NAME = HOSTNAME
HA_NAME = HOSTNAME.replace(" ", "-").lower()
print(f"Using DEVICE_NAME='{DEVICE_NAME}' and HA_NAME='{HA_NAME}' for MQTT topics")

TIMEOUT_SECONDS = int(os.getenv("LAST_TIMEOUT_SET", os.getenv("TIMEOUT_SECONDS", "300")))
DIMMING_TO_OFF_SECONDS = int(os.getenv("DIMMING_TO_OFF_SECONDS", "30"))
DIMMING_PERCENT = int(os.getenv("DIMMING_PERCENT", "20"))

# MQTT topics
HA_DIMMING_PERCENT_DISCOVERY_PREFIX = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_dimming_percent/config"
HA_DIMMING_PERCENT_STATE_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_dimming_percent/state"
HA_DIMMING_PERCENT_COMMAND_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_dimming_percent/set"

HA_DIMMING_TIMEOUT_DISCOVERY_PREFIX = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_dimming_timeout/config"
HA_DIMMING_TIMEOUT_STATE_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_dimming_timeout/state"
HA_DIMMING_TIMEOUT_COMMAND_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_dimming_timeout/set"

HA_LIGHT_DISCOVERY_PREFIX = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/config"
HA_LIGHT_STATE_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/state"
HA_LIGHT_BRIGHTNESS_STATE_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/brightness"
HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/brightness/set"
HA_LIGHT_COMMAND_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/set"

HA_TIMEOUT_NUMBER_DISCOVERY_PREFIX = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_timeout/config"
HA_TIMEOUT_NUMBER_STATE_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_timeout/state"
HA_TIMEOUT_NUMBER_COMMAND_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_timeout/set"

HA_UNDERVOLTAGE_DISCOVERY_PREFIX = f"homeassistant/sensor/{DEVICE_NAME}/{HA_NAME}_undervoltage/config"
HA_UNDERVOLTAGE_STATE_TOPIC = f"homeassistant/sensor/{DEVICE_NAME}/{HA_NAME}_undervoltage/state"

# State variables
current_state = "OFF"
current_brightness = 0
last_brightness = 100
last_activity = 0

def get_backlight_brightness_in_percent():
    path = f"/sys/class/backlight/{DISPLAY_NAME}/brightness"
    try:
        with open(path, "r") as f:
            raw_brightness = int(f.read().strip())
            percent_brightness = correlate_percent(raw=raw_brightness)
            return percent_brightness
    except Exception as e:
        print(f"Error reading brightness: {e}")
        return 0

def set_backlight_brightness_in_percent(value):
    path = f"/sys/class/backlight/{DISPLAY_NAME}/brightness"
    new_value = int(correlate_percent(percent=int(value)))
    if new_value < 3:
        new_value = 0
    cmd = f"echo {new_value} | sudo tee {path} > /dev/null"
    print(f"Set brightness to {value}% (raw: {new_value})")
    try:
        subprocess.call(cmd, shell=True)
        if int(value) >= 3:
            global last_activity
            last_activity = time.time()
    except Exception as e:
        print(f"Error setting brightness: {e}")

# MQTT callbacks
def on_connect(client, userdata, flags, rc, properties=None):
    global last_activity, mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print(f"Connected with result code {rc}")

        # Publish retained states FIRST (critical for number entities to show value)
        print("Publishing initial retained states...")
        client.publish(HA_TIMEOUT_NUMBER_STATE_TOPIC, str(TIMEOUT_SECONDS), retain=True)
        client.publish(HA_DIMMING_PERCENT_STATE_TOPIC, str(DIMMING_PERCENT), retain=True)
        client.publish(HA_DIMMING_TIMEOUT_STATE_TOPIC, str(DIMMING_TO_OFF_SECONDS), retain=True)

        # Publish undervoltage state immediately so sensor appears with value
        undervoltage = get_undervoltage_status()
        client.publish(HA_UNDERVOLTAGE_STATE_TOPIC, undervoltage, retain=True)

        # Then publish discovery (now includes undervoltage + light)
        publish_ha_light_discovery()

        # Subscribe
        client.subscribe(HA_LIGHT_COMMAND_TOPIC)
        client.subscribe(HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC)
        client.subscribe(HA_TIMEOUT_NUMBER_COMMAND_TOPIC)
        client.subscribe(HA_DIMMING_PERCENT_COMMAND_TOPIC)
        client.subscribe(HA_DIMMING_TIMEOUT_COMMAND_TOPIC)
    else:
        mqtt_connected = False
        print(f"Connection failed with code {rc}: {mqtt.error_string(rc)}")

def on_disconnect(client, userdata, rc, properties=None):
    print(f"Disconnected with result code {rc}")
    global mqtt_connected
    mqtt_connected = False
    if rc != 0:
        print("Unexpected disconnection. Will attempt reconnect...")

def on_message(client, userdata, msg):
    topic = msg.topic
    payload_str = msg.payload.decode("utf-8").strip()
    try:
        if topic == HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC:
            brightness = int(payload_str)
            print(f"Received brightness command: {brightness}%")
            process_command({"brightness": brightness})
        elif topic == HA_LIGHT_COMMAND_TOPIC:
            command = json.loads(payload_str)
            process_command(command)
        elif topic == HA_TIMEOUT_NUMBER_COMMAND_TOPIC:
            timeout_val = int(float(payload_str))
            set_timeout_seconds(timeout_val)
        elif topic == HA_DIMMING_TIMEOUT_COMMAND_TOPIC:
            dimming_val = int(float(payload_str))
            set_dimming_timeout_seconds(dimming_val)
        elif topic == HA_DIMMING_PERCENT_COMMAND_TOPIC:
            percent_val = int(float(payload_str))
            set_dimming_percent(percent_val)
    except Exception as e:
        print(f"Error processing message on {topic}: {e} (payload: {payload_str})")

# Update functions
def set_dimming_percent(new_percent):
    global DIMMING_PERCENT
    DIMMING_PERCENT = max(1, min(100, int(new_percent)))
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    set_key(env_path, 'DIMMING_PERCENT', str(DIMMING_PERCENT))
    client.publish(HA_DIMMING_PERCENT_STATE_TOPIC, str(DIMMING_PERCENT), retain=True)
    print(f"Dimming percent updated to {DIMMING_PERCENT}%")
    publish_ha_light_state()

def set_dimming_timeout_seconds(new_timeout):
    global DIMMING_TO_OFF_SECONDS
    DIMMING_TO_OFF_SECONDS = max(1, min(600, int(new_timeout)))
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    set_key(env_path, 'DIMMING_TO_OFF_SECONDS', str(DIMMING_TO_OFF_SECONDS))
    client.publish(HA_DIMMING_TIMEOUT_STATE_TOPIC, str(DIMMING_TO_OFF_SECONDS), retain=True)
    print(f"Dimming timeout updated to {DIMMING_TO_OFF_SECONDS}s")
    publish_ha_light_state()

def set_timeout_seconds(new_timeout):
    global TIMEOUT_SECONDS
    TIMEOUT_SECONDS = max(10, min(3600, int(new_timeout)))
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    set_key(env_path, 'LAST_TIMEOUT_SET', str(TIMEOUT_SECONDS))
    client.publish(HA_TIMEOUT_NUMBER_STATE_TOPIC, str(TIMEOUT_SECONDS), retain=True)
    print(f"Timeout updated to {TIMEOUT_SECONDS}s")
    publish_ha_light_state()

def process_command(command):
    global current_state, current_brightness, last_brightness, last_activity
    brightness = command.get("brightness")
    state = command.get("state")

    if brightness is not None:
        level = max(0, min(100, int(brightness)))
        new_state = "ON" if level > 0 else "OFF"
    else:
        if state == "ON":
            level = last_brightness
            new_state = "ON"
        elif state == "OFF":
            level = 0
            new_state = "OFF"
        else:
            print("Invalid command")
            return

    if current_state == "ON" and new_state == "OFF":
        last_brightness = current_brightness

    print(f"Processing: state={state}, brightness={brightness} → level={level}%, new_state={new_state}")
    set_backlight_brightness_in_percent(level)
    current_brightness = level
    current_state = new_state

    if current_state == "ON" or level > 2:
        last_activity = time.time()

    publish_ha_light_state()

# Discovery - now includes undervoltage sensor + light entity
def publish_ha_light_discovery():
    # Dimming percent
    dimming_percent_config = {
        "name": "HAUI Dimming Percent",
        "unique_id": f"{HA_NAME}_dimming_percent",
        "device": {"identifiers": [DEVICE_NAME], "name": HA_NAME, "manufacturer": "Custom", "model": "Display controller", "sw_version": "1.0"},
        "state_topic": HA_DIMMING_PERCENT_STATE_TOPIC,
        "command_topic": HA_DIMMING_PERCENT_COMMAND_TOPIC,
        "unit_of_measurement": "%",
        "icon": "mdi:brightness-6",
        "entity_category": "config",
        "min": 1, "max": 100, "step": 1, "mode": "box"
    }
    client.publish(HA_DIMMING_PERCENT_DISCOVERY_PREFIX, json.dumps(dimming_percent_config), retain=True)
    print(f"Published dimming percent discovery")

    # Dimming timeout
    dimming_timeout_config = {
        "name": "HAUI Dimming Timeout",
        "unique_id": f"{HA_NAME}_dimming_timeout",
        "device": {"identifiers": [DEVICE_NAME], "name": HA_NAME, "manufacturer": "Custom", "model": "Display controller", "sw_version": "1.0"},
        "state_topic": HA_DIMMING_TIMEOUT_STATE_TOPIC,
        "command_topic": HA_DIMMING_TIMEOUT_COMMAND_TOPIC,
        "unit_of_measurement": "s",
        "icon": "mdi:timer-sand",
        "entity_category": "config",
        "min": 1, "max": 600, "step": 1, "mode": "box"
    }
    client.publish(HA_DIMMING_TIMEOUT_DISCOVERY_PREFIX, json.dumps(dimming_timeout_config), retain=True)
    print(f"Published dimming timeout discovery")

    # Timeout number
    timeout_number_config = {
        "name": "HAUI Backlight Timeout",
        "unique_id": f"{HA_NAME}_backlight_timeout",
        "device": {"identifiers": [DEVICE_NAME], "name": HA_NAME, "manufacturer": "Custom", "model": "Display controller", "sw_version": "1.0"},
        "state_topic": HA_TIMEOUT_NUMBER_STATE_TOPIC,
        "command_topic": HA_TIMEOUT_NUMBER_COMMAND_TOPIC,
        "unit_of_measurement": "s",
        "icon": "mdi:timer",
        "entity_category": "config",
        "min": 10, "max": 3600, "step": 1, "mode": "box"
    }
    client.publish(HA_TIMEOUT_NUMBER_DISCOVERY_PREFIX, json.dumps(timeout_number_config), retain=True)
    print(f"Published timeout number discovery")

    # Undervoltage sensor (FIX: was missing, so entity never appeared in HA)
    undervoltage_config = {
        "name": "HAUI Undervoltage",
        "unique_id": f"{HA_NAME}_undervoltage",
        "device": {"identifiers": [DEVICE_NAME], "name": HA_NAME, "manufacturer": "Custom", "model": "Display controller", "sw_version": "1.0"},
        "state_topic": HA_UNDERVOLTAGE_STATE_TOPIC,
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic",
        "device_class": "problem",
        "payload_on": "1",
        "payload_off": "0"
    }
    client.publish(HA_UNDERVOLTAGE_DISCOVERY_PREFIX, json.dumps(undervoltage_config), retain=True)
    print(f"Published undervoltage discovery")

    # Light entity
    light_config = {
        "name": "HAUI Backlight",
        "unique_id": f"{HA_NAME}_backlight",
        "device": {"identifiers": [DEVICE_NAME], "name": HA_NAME, "manufacturer": "Custom", "model": "Display controller", "sw_version": "1.0"},
        "state_topic": HA_LIGHT_STATE_TOPIC,
        "command_topic": HA_LIGHT_COMMAND_TOPIC,
        "brightness_state_topic": HA_LIGHT_BRIGHTNESS_STATE_TOPIC,
        "brightness_command_topic": HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC,
        "brightness_scale": 100,
        "icon": "mdi:monitor",
        "supported_color_modes": ["brightness"],
        "color_mode": "brightness"
    }
    client.publish(HA_LIGHT_DISCOVERY_PREFIX, json.dumps(light_config), retain=True)
    print(f"Published light discovery")

# State publishing
def publish_ha_light_state():
    try:
        state_data = {"state": current_state, "brightness": current_brightness}
        client.publish(HA_LIGHT_STATE_TOPIC, json.dumps(state_data), retain=True)
        client.publish(HA_LIGHT_BRIGHTNESS_STATE_TOPIC, str(current_brightness), retain=True)
        client.publish(HA_TIMEOUT_NUMBER_STATE_TOPIC, str(TIMEOUT_SECONDS), retain=True)
        client.publish(HA_DIMMING_PERCENT_STATE_TOPIC, str(DIMMING_PERCENT), retain=True)
        client.publish(HA_DIMMING_TIMEOUT_STATE_TOPIC, str(DIMMING_TO_OFF_SECONDS), retain=True)

        undervoltage = get_undervoltage_status()
        client.publish(HA_UNDERVOLTAGE_STATE_TOPIC, undervoltage, retain=True)

        print(f"Published states: timeout={TIMEOUT_SECONDS}s, dim %={DIMMING_PERCENT}%, dim to off={DIMMING_TO_OFF_SECONDS}s, undervoltage={undervoltage}")
    except Exception as e:
        print(f"Error publishing state: {e}")

def get_undervoltage_status():
    try:
        result = subprocess.check_output(["vcgencmd", "get_throttled"]).decode("utf-8").strip()
        hex_val = result.split('=')[1]
        val = int(hex_val, 16)
        return "1" if (val & 0x1) else "0"
    except Exception:
        return "error"

# Touch monitor thread
def touch_monitor():
    global current_state, current_brightness, last_brightness, last_activity
    try:
        device = InputDevice(TOUCH_DEVICE)
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH and event.value == 1:
                set_backlight_brightness_in_percent(last_brightness)
                current_brightness = last_brightness
                current_state = "ON"
                last_activity = time.time()
                publish_ha_light_state()
    except Exception:
        pass


# MQTT setup with CA cert check
client = mqtt.Client(protocol=mqtt.MQTTv311)
if os.path.isfile(CA_CERT):
    client.tls_set(ca_certs=CA_CERT, cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
    print(f"Using CA certificate at {CA_CERT} for TLS connection.")
else:
    print(f"CA certificate not found at {CA_CERT}. Connecting without TLS (username/password only).")
    if USERNAME and PASSWORD:
        client.username_pw_set(USERNAME, PASSWORD)
client.on_connect = on_connect
client.on_message = on_message
client.on_disconnect = on_disconnect

try:
    client.connect(BROKER, PORT)
except Exception as e:
    print(f"Connection error: {e}")

client.loop_start()

# Initial retained state publish (safety net)
time.sleep(2)
print("Initial state publish (safety)...")
client.publish(HA_TIMEOUT_NUMBER_STATE_TOPIC, str(TIMEOUT_SECONDS), retain=True)
client.publish(HA_DIMMING_PERCENT_STATE_TOPIC, str(DIMMING_PERCENT), retain=True)
client.publish(HA_DIMMING_TIMEOUT_STATE_TOPIC, str(DIMMING_TO_OFF_SECONDS), retain=True)
undervoltage = get_undervoltage_status()
client.publish(HA_UNDERVOLTAGE_STATE_TOPIC, undervoltage, retain=True)

# Start touch monitoring
threading.Thread(target=touch_monitor, daemon=True).start()

# Main loop
last_republish = 0
dim_start_time = None
last_mqtt_attempt = 0
MQTT_RECONNECT_INTERVAL = 30

try:
    while True:
        now = time.time()

        if not mqtt_connected and now - last_mqtt_attempt > MQTT_RECONNECT_INTERVAL:
            try:
                client.reconnect()
                print("MQTT reconnect attempt...")
            except Exception as e:
                print(f"Reconnect failed: {e}")
            last_mqtt_attempt = now

        if mqtt_connected and now - last_republish > 600:
            publish_ha_light_discovery()  # republish discovery periodically
            last_republish = now

        # External brightness change detection
        current_level = get_backlight_brightness_in_percent()
        if current_level != current_brightness:
            print(f"External brightness change: {current_level}%")
            current_brightness = current_level
            if current_level is None:
                current_level = 0
            new_state = "ON" if current_level > 0 else "OFF"
            if current_state == "ON" and new_state == "OFF":
                last_brightness = current_brightness
            current_state = new_state
            if current_state == "ON":
                last_activity = now
            publish_ha_light_state()

        # Timeout → dim logic
        if current_state == "ON":
            if now - last_activity > TIMEOUT_SECONDS:
                print("Inactivity timeout → dimming")
                last_brightness = current_brightness
                dim_percent = max(1, int(DIMMING_PERCENT))
                set_backlight_brightness_in_percent(dim_percent)
                current_brightness = dim_percent
                publish_ha_light_state()
                dim_start_time = now
                current_state = "DIMMED"

        # Dimmed → off logic
        # FIX: if dimming timeout is set to max (600), never turn backlight to zero
        if (current_state == "DIMMED" and dim_start_time is not None
                and DIMMING_TO_OFF_SECONDS < 600
                and now - dim_start_time > DIMMING_TO_OFF_SECONDS):
            print("Dim period over → turning off")
            set_backlight_brightness_in_percent(0)
            current_brightness = 0
            current_state = "OFF"
            publish_ha_light_state()
            dim_start_time = None

        time.sleep(1)

except KeyboardInterrupt:
    print("Stopping...")
finally:
    client.loop_stop()
    client.disconnect()
