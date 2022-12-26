# Note from Faisal - this code is almost entirely stolen from Anton's Mindstorms & Ste7an
# Their copyright is below.
# Made some mods here and there to control from Windows. Originally code was meant to
# control from another Spike brick.

# Don't use V3 of the Spike Prime app for now, run on V2. 
# (You might have to downgrade if you already upgraded to V3. There's a tool to do that here):
# https://spikelegacy.legoeducation.com/hubdowngrade/#step-1
# Read the instructions carefully -> you'll need a mac if you have to downgrade the brick.


# This is the car code for the remote controlled Tank-like vehicle
# Build one by slapping two motors on the side of your hub.
# (c) 2021 Anton's Mindstorms & Ste7an

# Use with the the remote control tutorial here:
# [url]

# Most of it is library bluetooth code.
# Scroll to line 200 for the core program.


# ===== Move this to a library import someday ====== #
from hub import display, Image, sound
from spike import PrimeHub
import bluetooth
import random
import struct
import time
from time import sleep_ms
from micropython import const
from machine import Timer

_CONNECT_IMAGES = [
    Image('03579:00000:00000:00000:00000'),
    Image('00357:00000:00000:00000:00000'),
    Image('00035:00000:00000:00000:00000'),
    Image('00003:00000:00000:00000:00000'),
    Image('00000:00000:00000:00000:00009'),
    Image('00000:00000:00000:00000:00097'),
    Image('00000:00000:00000:00000:00975'),
    Image('00000:00000:00000:00000:09753'),
    Image('00000:00000:00000:00000:97530'),
    Image('00000:00000:00000:00000:75300'),
    Image('00000:00000:00000:00000:53000'),
    Image('90000:00000:00000:00000:30000'),
    Image('79000:00000:00000:00000:00000'),
    Image('57900:00000:00000:00000:00000'),
    Image('35790:00000:00000:00000:00000'),
]

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2

if 'FLAG_INDICATE' in dir(bluetooth):
    # We're on MINDSTORMS Robot Inventor
    # New version of bluetooth
    _IRQ_GATTS_WRITE = 3
else:
    # We're probably on SPIKE Prime
    _IRQ_GATTS_WRITE = 1<<2

_FLAG_READ = const(0x0002)
_FLAG_WRITE_NO_RESPONSE = const(0x0004)
_FLAG_WRITE = const(0x0008)
_FLAG_NOTIFY = const(0x0010)

# Helpers for generating BLE advertising payloads.
# Advertising payloads are repeated packets of the following form:
#1 byte data length (N + 1)
#1 byte type (see constants below)
#N bytes type-specific data

_ADV_TYPE_FLAGS = const(0x01)
_ADV_TYPE_NAME = const(0x09)
_ADV_TYPE_UUID16_COMPLETE = const(0x3)
_ADV_TYPE_UUID32_COMPLETE = const(0x5)
_ADV_TYPE_UUID128_COMPLETE = const(0x7)
_ADV_TYPE_UUID16_MORE = const(0x2)
_ADV_TYPE_UUID32_MORE = const(0x4)
_ADV_TYPE_UUID128_MORE = const(0x6)
_ADV_TYPE_APPEARANCE = const(0x19)


_UART_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
_UART_TX = (
    bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"),
    _FLAG_READ | _FLAG_NOTIFY,
)
_UART_RX = (
    bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
    _FLAG_WRITE | _FLAG_WRITE_NO_RESPONSE,
)
_UART_SERVICE = (
    _UART_UUID,
    (_UART_TX, _UART_RX),
)


# Generate a payload to be passed to gap_advertise(adv_data=...).
def advertising_payload(limited_disc=False, br_edr=False, name=None, services=None, appearance=0):
    payload = bytearray()

    def _append(adv_type, value):
        nonlocal payload
        payload += struct.pack("BB", len(value) + 1, adv_type) + value

    _append(
        _ADV_TYPE_FLAGS,
        struct.pack("B", (0x01 if limited_disc else 0x02) + (0x18 if br_edr else 0x04)),
    )

    if name:
        _append(_ADV_TYPE_NAME, name)

    if services:
        for uuid in services:
            b = bytes(uuid)
            if len(b) == 2:
                _append(_ADV_TYPE_UUID16_COMPLETE, b)
            elif len(b) == 4:
                _append(_ADV_TYPE_UUID32_COMPLETE, b)
            elif len(b) == 16:
                _append(_ADV_TYPE_UUID128_COMPLETE, b)

    # See org.bluetooth.characteristic.gap.appearance.xml
    if appearance:
        _append(_ADV_TYPE_APPEARANCE, struct.pack("<h", appearance))

    return payload


def decode_field(payload, adv_type):
    i = 0
    result = []
    while i + 1 < len(payload):
        if payload[i + 1] == adv_type:
            result.append(payload[i + 2 : i + payload[i] + 1])
        i += 1 + payload[i]
    return result


def decode_name(payload):
    n = decode_field(payload, _ADV_TYPE_NAME)
    return str(n[0], "utf-8") if n else ""


def decode_services(payload):
    services = []
    for u in decode_field(payload, _ADV_TYPE_UUID16_COMPLETE):
        services.append(bluetooth.UUID(struct.unpack("<h", u)[0]))
    for u in decode_field(payload, _ADV_TYPE_UUID32_COMPLETE):
        services.append(bluetooth.UUID(struct.unpack("<d", u)[0]))
    for u in decode_field(payload, _ADV_TYPE_UUID128_COMPLETE):
        services.append(bluetooth.UUID(u))
    return services


class BLESimplePeripheral:
    def __init__(self, name="robot", logo="00000:05550:05950:05550:00000", ble=None):
        self._n=12
        self._logo=Image(logo)
        self._CONNECT_ANIMATION = [img + self._logo for img in _CONNECT_IMAGES]
        if ble==None:
            ble = bluetooth.BLE()
        self._ble = ble
        self._ble.active(True)
        self._ble.irq(self._irq)
        ((self._handle_tx, self._handle_rx),) = self._ble.gatts_register_services((_UART_SERVICE,))
        self._connections = set()
        self._connected=False
        self._write_callback = None
        self._update_animation()
        self._payload = advertising_payload(name=name, services=[_UART_UUID])
        self._advertise()

    def _irq(self, event, data):
        # Track connections so we can send notifications.
        if event == _IRQ_CENTRAL_CONNECT:
            conn_handle, _, _ = data
            print("New connection", conn_handle)
            self._connections.add(conn_handle)
            self._connected=True
            self._update_animation()
            sleep_ms(300)
            #t = Timer(mode=Timer.ONE_SHOT, period=2000, callback=lambda x:self.send(repr(self._logo)))

        elif event == _IRQ_CENTRAL_DISCONNECT:
            conn_handle, _, _ = data
            print("Disconnected", conn_handle)
            self._connections.remove(conn_handle)
            self._connected=False
            self._update_animation()
            # Start advertising again to allow a new connection.
            self._advertise()
        elif event == _IRQ_GATTS_WRITE:
            conn_handle, value_handle = data
            value = self._ble.gatts_read(value_handle)
            if value_handle == self._handle_rx and self._write_callback:
                self._write_callback(value)

    def send(self, data):
        for conn_handle in self._connections:
            self._ble.gatts_notify(conn_handle, self._handle_tx, data)

    def is_connected(self):
        return len(self._connections) > 0

    def _advertise(self, interval_us=100000):
        print("Starting advertising")
        self._ble.gap_advertise(interval_us, adv_data=self._payload)

    def on_write(self, callback):
        self._write_callback = callback

    def _update_animation(self):
        if not self._connected:
            display.show(self._CONNECT_ANIMATION, delay=100, wait=False, loop=True)
        else:
            display.show(self._logo)


# ===== End of library ===== #




# Imports for program
from hub import port, sound
from time import sleep_ms

# Intialize
receiver = BLESimplePeripheral(logo="00000:09990:00900:00900:00000") # T for tank
l_stick_ver, r_stick_ver, turret = [0]*3

# Remote control data callback function
def on_rx(control):
    global l_stick_ver, r_stick_ver, turret
    l_stick_ver, r_stick_ver, turret = struct.unpack("bbB", control)

receiver.on_write(on_rx)

# Motor helper functions
def clamp_int(n, floor=-100, ceiling=100):
    return max(min(int(n),ceiling),floor)

def track_target(motor, target=0, gain=1.5):
    m_pos = motor.get()[1]
    motor.pwm(
        clamp_int((m_pos-target)*-gain)
    )
    return m_pos

# Uncomment this to enable linkage steering on motors E and F
# Define motors here:
# forward = port.F.motor
# steer_left = port.E.motor

# Uncomment to enable tank steering on motors A and B
left_motor = port.E.motor
right_motor = port.F.motor

hub = PrimeHub()
did_connect = False
did_disconnect = False
# Control loop
while True:
    if receiver.is_connected():
        if not did_connect:
            hub.speaker.beep(72,0.05)
            hub.speaker.beep(84,0.05)
            hub.speaker.beep(72,0.05)
            hub.speaker.beep(84,0.05)
            hub.speaker.beep(96,0.05)
            did_connect = True
            did_disconnect = False

        left_motor.pwm( r_stick_ver )
        right_motor.pwm( -1*l_stick_ver )

    else:
        if not did_disconnect:
            hub.speaker.beep(96,0.05)
            hub.speaker.beep(84,0.05)
            hub.speaker.beep(72,0.05)
            hub.speaker.beep(84,0.05)
            hub.speaker.beep(72,0.05)
            did_disconnect = True
            did_connect = False

        # Turn off motors when no remote is connected
        port.E.pwm(0)
        port.F.pwm(0)


    # Limit control loop speed for bluetooth messages to have time to arrive
    sleep_ms(20)
