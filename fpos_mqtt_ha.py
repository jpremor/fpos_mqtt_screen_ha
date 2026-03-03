import paho.mqtt.client as mqtt
import json
import time
import ssl
import os
from dotenv import load_dotenv
import subprocess
import threading
from evdev import InputDevice, ecodes

from dotenv import set_key
# Load environment variables
load_dotenv()
BROKER = os.getenv("BROKER_IP")
PORT = int(os.getenv("BROKER_PORT"))
USERNAME = os.getenv("BROKER_USERNAME")
PASSWORD = os.getenv("BROKER_PASSWORD")
CA_CERT = os.path.join(os.path.dirname(__file__), "ca.crt")
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "10-0045")
DIMMING_PERCENT = int(os.getenv("DIMMING_PERCENT", "20"))

# Dynamically find the touch device event number based on DISPLAY_DEVICE_NAME
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
# Load default timeout from .env
TIMEOUT_SECONDS = int(os.getenv("LAST_TIMEOUT_SET", os.getenv("TIMEOUT_SECONDS", "300")))
DIMMING_TO_OFF_SECONDS = int(os.getenv("DIMMING_TO_OFF_SECONDS", "30"))

# Track MQTT connection status
mqtt_connected = False
DIMMING_TO_OFF_SECONDS = os.getenv("DIMMING_TO_OFF_SECONDS", "5")

# Home Assistant friendly identifiers (no spaces in DEVICE_NAME)
DEVICE_NAME = "BasementUI"
HA_NAME = "basement_ui"

HA_LIGHT_DISCOVERY_PREFIX = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/config"
HA_LIGHT_STATE_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/state"
HA_LIGHT_BRIGHTNESS_STATE_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/brightness"
HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/brightness/set"
HA_LIGHT_COMMAND_TOPIC = f"homeassistant/light/{DEVICE_NAME}/{HA_NAME}/set"
# Timeout sensor topics
HA_TIMEOUT_DISCOVERY_PREFIX = f"homeassistant/sensor/{DEVICE_NAME}/{HA_NAME}_timeout/config"
HA_TIMEOUT_STATE_TOPIC = f"homeassistant/sensor/{DEVICE_NAME}/{HA_NAME}_timeout/state"
# Timeout number entity topics
HA_TIMEOUT_NUMBER_DISCOVERY_PREFIX = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_timeout/config"
HA_TIMEOUT_NUMBER_STATE_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_timeout/state"
HA_TIMEOUT_NUMBER_COMMAND_TOPIC = f"homeassistant/number/{DEVICE_NAME}/{HA_NAME}_timeout/set"
# Undervoltage sensor topics
HA_UNDERVOLTAGE_DISCOVERY_PREFIX = f"homeassistant/sensor/{DEVICE_NAME}/{HA_NAME}_undervoltage/config"
HA_UNDERVOLTAGE_STATE_TOPIC = f"homeassistant/sensor/{DEVICE_NAME}/{HA_NAME}_undervoltage/state"

# State variables
current_state = "OFF"
current_brightness = 0
last_brightness = 100
last_activity = 0

def get_backlight_brightness():
    path = f"/sys/class/backlight/{DISPLAY_NAME}/brightness"
    try:
        with open(path, "r") as f:
            read_brightness = int(f.read().strip())
            print(f"Read brightness from system {read_brightness * 100 // 255}% (raw: {read_brightness})")
            return int(read_brightness * 100 // 255 )
    except Exception as e:
        print(f"Error reading brightness: {e}")
        return 0

def set_backlight_brightness_in_percent(value):
    path = f"/sys/class/backlight/{DISPLAY_NAME}/brightness"
    new_value = int(int(value) * 255 // 100)
    cmd = f"echo {new_value} | sudo tee {path}"
    try:
        subprocess.call(cmd, shell=True)
        # Reset timeout if brightness is set above 1% (value > 2)
        print(f"Set brightness to {value}% (raw: {new_value})")
        if int(value) > 2:
            global last_activity
            last_activity = time.time()
    except Exception as e:
        print(f"Error setting brightness: {e}")

# Callback when connected to MQTT broker
def on_connect(client, userdata, flags, rc, properties=None):
    global last_activity
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print(f"Connected with result code {rc}")
        # Publish discovery
        publish_ha_light_discovery()
        # Initialize state
        current_level = get_backlight_brightness()
        if current_level > 0:
            current_state = "ON"
            current_brightness = current_level
            last_brightness = current_level
            last_activity = time.time()
        else:
            current_state = "OFF"
            current_brightness = 0
            last_activity = 0
        publish_ha_light_state()
        # Subscribe to commands
        client.subscribe(HA_LIGHT_COMMAND_TOPIC)
        client.subscribe(HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC)
        client.subscribe(HA_TIMEOUT_NUMBER_COMMAND_TOPIC)
    else:
        mqtt_connected = False
        print(f"Connection failed with code {rc}: {mqtt.error_string(rc)}")

# Callback when disconnected
def on_disconnect(client, userdata, rc, properties=None):
    print(f"Disconnected with result code {rc}")
    global mqtt_connected
    mqtt_connected = False
    if rc != 0:
        print("Unexpected disconnection. Reconnecting...")
        try:
            client.reconnect()
        except Exception as e:
            print(f"Reconnection failed: {e}")

# Callback when message received
def on_message(client, userdata, msg):
    topic = msg.topic
    payload_str = msg.payload.decode("utf-8").strip()

    if topic == HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC:
        try:
            brightness = int(payload_str)
            process_command({"brightness": brightness})
        except ValueError:
            print(f"Invalid brightness: {payload_str}")
    elif topic == HA_LIGHT_COMMAND_TOPIC:
        try:
            command = json.loads(payload_str)
            process_command(command)
        except json.JSONDecodeError:
            print(f"Invalid JSON command: {payload_str}")
    elif topic == HA_TIMEOUT_NUMBER_COMMAND_TOPIC:
        try:
            timeout_val = int(float(payload_str))
            set_timeout_seconds(timeout_val)
        except ValueError:
            print(f"Invalid timeout value: {payload_str}")

def process_command(command):
    global current_state, current_brightness, last_brightness, last_activity
    brightness = command.get("brightness")
    state = command.get("state")

    if brightness is not None:
        level = max(0, min(100, int(brightness)))
        new_state = "ON" if level > 0 else "OFF"
    else:
        dim_start_time = None
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

    set_backlight_brightness_in_percent(level)
    current_brightness = level
    current_state = new_state
    # Reset timeout if brightness is set above 1% or turned on
    if current_state == "ON" or level > 2:
        last_activity = time.time()
    publish_ha_light_state()

# Discovery payload
def publish_ha_light_discovery():
    # Undervoltage sensor discovery
    undervoltage_config = {
        "name": "Basement UI Undervoltage",
        "unique_id": f"basement_ui_undervoltage",
        "device": {
            "identifiers": [DEVICE_NAME],
            "name": "Basement UI",
            "manufacturer": "Raspberry Pi",
            "model": "Pi",
            "sw_version": "1.0"
        },
        "state_topic": HA_UNDERVOLTAGE_STATE_TOPIC,
        "icon": "mdi:flash-alert",
        "entity_category": "diagnostic",
        "value_template": "{{ value }}"
    }
    client.publish(HA_UNDERVOLTAGE_DISCOVERY_PREFIX, json.dumps(undervoltage_config), retain=True)
    print(f"Published undervoltage sensor discovery to: {HA_UNDERVOLTAGE_DISCOVERY_PREFIX}")
    # Light entity discovery
    config = {
        "name": "Basement UI Backlight",
        "unique_id": f"basement_ui_backlight",
        "device": {
            "identifiers": [DEVICE_NAME],
            "name": "Basement UI",
            "manufacturer": "Custom",
            "model": "Display controller",
            "sw_version": "1.0"
        },
        "state_topic": HA_LIGHT_STATE_TOPIC,
        "command_topic": HA_LIGHT_COMMAND_TOPIC,
        "payload_on": "ON",
        "payload_off": "OFF",
        "brightness_state_topic": HA_LIGHT_BRIGHTNESS_STATE_TOPIC,
        "brightness_command_topic": HA_LIGHT_BRIGHTNESS_COMMAND_TOPIC,
        "brightness_scale": 100,
        "brightness": True,
        "supported_color_modes": ["brightness"],
        "schema": "json",
        "optimistic": False
    }
    client.publish(HA_LIGHT_DISCOVERY_PREFIX, json.dumps(config), retain=True)
    print(f"Published light discovery to: {HA_LIGHT_DISCOVERY_PREFIX}")

    # Timeout number entity discovery
    timeout_number_config = {
        "name": "Basement UI Backlight Timeout",
        "unique_id": f"basement_ui_backlight_timeout",
        "device": {
            "identifiers": [DEVICE_NAME],
            "name": "Basement UI",
            "manufacturer": "Custom",
            "model": "Display controller",
            "sw_version": "1.0"
        },
        "state_topic": HA_TIMEOUT_NUMBER_STATE_TOPIC,
        "command_topic": HA_TIMEOUT_NUMBER_COMMAND_TOPIC,
        "unit_of_measurement": "s",
        "icon": "mdi:timer",
        "entity_category": "config",
        "min": 10,
        "max": 3600,
        "step": 1,
        "mode": "box"
    }
    client.publish(HA_TIMEOUT_NUMBER_DISCOVERY_PREFIX, json.dumps(timeout_number_config), retain=True)
    print(f"Published timeout number discovery to: {HA_TIMEOUT_NUMBER_DISCOVERY_PREFIX}")

# State publishing
def publish_ha_light_state():
    try:
        # Publish combined JSON state payload (required for schema=json)
        state_data = {
            "state": current_state,
            "brightness": current_brightness
        }
        client.publish(HA_LIGHT_STATE_TOPIC, json.dumps(state_data), retain=True)

        # Publish timeout value
        client.publish(HA_TIMEOUT_NUMBER_STATE_TOPIC, str(TIMEOUT_SECONDS), retain=True)

        # Publish undervoltage status
        undervoltage = get_undervoltage_status()
        client.publish(HA_UNDERVOLTAGE_STATE_TOPIC, undervoltage, retain=True)

    except Exception as e:
        print(f"Error publishing light state: {e}")

# Function to get undervoltage status using vcgencmd
def get_undervoltage_status():
    try:
        result = subprocess.check_output(["vcgencmd", "get_throttled"]).decode("utf-8").strip()
        # Example output: 'throttled=0x50000'
        hex_val = result.split('=')[1]
        val = int(hex_val, 16)
        # Bit 0: under-voltage detected
        return "1" if (val & 0x1) else "0"
    except Exception as e:
        print(f"Error reading undervoltage status: {e}")
        return "error"

# Add function to update timeout from HA

def set_timeout_seconds(new_timeout):
    global TIMEOUT_SECONDS
    TIMEOUT_SECONDS = max(10, min(3600, int(new_timeout)))
    # Save to .env
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    set_key(env_path, 'LAST_TIMEOUT_SET', str(TIMEOUT_SECONDS))
    client.publish(HA_TIMEOUT_NUMBER_STATE_TOPIC, str(TIMEOUT_SECONDS), retain=True)
    publish_ha_light_state()

# Republish discovery periodically
def republish_all():
    publish_ha_light_discovery()

def touch_monitor():
    global current_state, current_brightness, last_brightness, last_activity
    try:
        try:
            device = InputDevice(TOUCH_DEVICE)
        except Exception:
            return
        for event in device.read_loop():
            if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH and event.value == 1:
                set_backlight_brightness_in_percent(100)
                current_brightness = 100
                current_state = "ON"
                last_brightness = 100
                last_activity = time.time()
                publish_ha_light_state()
    except Exception:
        pass

# MQTT client setup
client = mqtt.Client(protocol=mqtt.MQTTv311)
client.tls_set(ca_certs=CA_CERT, cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
if USERNAME and PASSWORD:
    client.username_pw_set(USERNAME, PASSWORD)
client.on_connect = on_connect
client.on_message = on_message
client.on_disconnect = on_disconnect

try:
    client.connect(BROKER, PORT)
except Exception as e:
    print(f"Connection error: {e}")
    exit(1)

client.loop_start()

# Start touch monitoring in background
threading.Thread(target=touch_monitor, daemon=True).start()

# Main loop: republish + external change detection + timeout logic
last_republish = 0
dim_start_time = None
last_mqtt_attempt = 0
MQTT_RECONNECT_INTERVAL = 30  # seconds
try:
    while True:
        now = time.time()

        # Periodic MQTT reconnect attempt if not connected
        if not mqtt_connected and now - last_mqtt_attempt > MQTT_RECONNECT_INTERVAL:
            try:
                client.reconnect()
                print("Attempting MQTT reconnect...")
            except Exception as e:
                print(f"MQTT reconnect failed: {e}")
            last_mqtt_attempt = now

        # Periodic republish (only if MQTT is connected)
        if mqtt_connected and now - last_republish > 600:
            republish_all()
            last_republish = now

        # Detect external brightness changes
        current_level = get_backlight_brightness()
        if current_level != current_brightness:
            print(f"External brightness change detected: {current_level}")
            current_brightness = current_level
            new_state = "ON" if current_level > 0 else "OFF"
            if current_state == "ON" and new_state == "OFF":
                last_brightness = current_brightness
            current_state = new_state
            if current_state == "ON":
                last_activity = now
            publish_ha_light_state()

        # Timeout logic
        # If ON and timeout reached, dim to 10% and start dim timer
        if current_state == "ON" and now - last_activity > TIMEOUT_SECONDS:
            last_brightness = current_brightness
            dim_percent = max(1, int(DIMMING_PERCENT / 100))
            set_backlight_brightness_in_percent(dim_percent)
            current_brightness = dim_percent
            publish_ha_light_state()
            dim_start_time = now
            current_state = "DIMMED"

        # If DIMMED and dim period passed, turn off
        if current_state == "DIMMED" and dim_start_time is not None:
            if now - dim_start_time > int(DIMMING_TO_OFF_SECONDS):
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
