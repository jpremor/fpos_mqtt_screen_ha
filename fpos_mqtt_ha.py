import paho.mqtt.client as mqtt
import json
import time
import ssl
import os
from dotenv import load_dotenv
import subprocess
import threading
from evdev import InputDevice, ecodes

# Load environment variables
load_dotenv()
BROKER = os.getenv("BROKER_IP")
PORT = int(os.getenv("BROKER_PORT"))
USERNAME = os.getenv("BROKER_USERNAME")
PASSWORD = os.getenv("BROKER_PASSWORD")
CA_CERT = os.path.join(os.path.dirname(__file__), "ca.crt")
DISPLAY_NAME = os.getenv("DISPLAY_NAME", "10-0045")
TOUCH_DEVICE = os.getenv("TOUCH_DEVICE", "/dev/input/event5")
TIMEOUT_SECONDS = int(os.getenv("TIMEOUT_SECONDS", "300"))  # default 5 minutes

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
last_brightness = 255
last_activity = 0

def get_backlight_brightness():
    path = f"/sys/class/backlight/{DISPLAY_NAME}/brightness"
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except Exception as e:
        print(f"Error reading brightness: {e}")
        return 0

def set_backlight_brightness(value):
    path = f"/sys/class/backlight/{DISPLAY_NAME}/brightness"
    cmd = f"echo {value} | sudo tee {path}"
    try:
        subprocess.call(cmd, shell=True)
        # Reset timeout if brightness is set above 1% (value > 2)
        if int(value) > 2:
            global last_activity
            last_activity = time.time()
    except Exception as e:
        print(f"Error setting brightness: {e}")

# Callback when connected to MQTT broker
def on_connect(client, userdata, flags, rc, properties=None):
    global last_activity
    if rc == 0:
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
        print(f"Connection failed with code {rc}: {mqtt.error_string(rc)}")

# Callback when disconnected
def on_disconnect(client, userdata, rc, properties=None):
    print(f"Disconnected with result code {rc}")
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
        level = max(0, min(255, int(brightness)))
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

    set_backlight_brightness(level)
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
        "brightness_scale": 255,
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
        state_data = {"state": current_state}
        client.publish(HA_LIGHT_STATE_TOPIC, json.dumps(state_data), retain=True)
        if current_state == "ON":
            client.publish(HA_LIGHT_BRIGHTNESS_STATE_TOPIC, str(current_brightness), retain=True)
        # Always publish timeout value
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
    print(f"Timeout updated to {TIMEOUT_SECONDS}s from Home Assistant")
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
            print(f"Touch monitor started on {TOUCH_DEVICE}", flush=True)
        except Exception as e:
            print(f"Failed to open touch device {TOUCH_DEVICE}: {e}", flush=True)
            return
        for event in device.read_loop():
            print(f"Touch event: type={event.type}, code={event.code}, value={event.value}", flush=True)
            if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH and event.value == 1:
                print(f"Touch detected at {time.strftime('%Y-%m-%d %H:%M:%S')} → setting brightness to 255 and resetting timeout", flush=True)
                set_backlight_brightness(255)
                current_brightness = 255
                current_state = "ON"
                last_brightness = 255
                last_activity = time.time()
                publish_ha_light_state()
    except Exception as e:
        print(f"Touch monitor error: {e}")

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
try:
    while True:
        now = time.time()

        # Periodic republish
        if now - last_republish > 600:
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
        if current_state == "ON" and now - last_activity > TIMEOUT_SECONDS:
            print(f"Timeout ({TIMEOUT_SECONDS}s) reached → turning off")
            last_brightness = current_brightness
            set_backlight_brightness(0)
            current_brightness = 0
            current_state = "OFF"
            publish_ha_light_state()

        time.sleep(1)
except KeyboardInterrupt:
    print("Stopping...")
finally:
    client.loop_stop()
    client.disconnect()
