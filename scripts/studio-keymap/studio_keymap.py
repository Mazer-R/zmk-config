#!/usr/bin/env python3
"""
Back up and restore the keymap stored on a ZMK keyboard through the ZMK
Studio protocol.

ZMK Studio edits the keymap live on the keyboard, but has no export button.
This tool talks the same protocol over USB:

    python3 scripts/studio-keymap/studio_keymap.py export
    python3 scripts/studio-keymap/studio_keymap.py restore keymap-backups/<file>.json

Export writes two files into keymap-backups/ (in the repository root):

  * <timestamp>.json    Exact copy of every binding (behavior id + raw
                        parameters), plus enough context to restore it later.
  * <timestamp>.keymap  A complete, buildable keymap: your current
                        config/*.keymap with only the layers replaced by the
                        ones read from the keyboard, aligned like the board.

Restore writes a JSON backup back to the keyboard and saves it, like Save in
ZMK Studio. It only writes keys that differ, and --dry-run shows the changes
without writing anything.

Requirements: Python 3.8+ only (no pip packages). The keyboard must be
connected over USB and running firmware with ZMK Studio enabled. Reading
or writing the keymap needs the keyboard to be unlocked (press your
&studio_unlock key); the tool waits for it.

Protocol reference: zmk-studio-messages (proto/zmk/*.proto) and
zmk/app/src/studio/ in ZMK v0.3. See README.md next to this file.
"""

import argparse
import datetime
import glob
import json
import os
import re
import select
import sys
import termios
import time
import tty

# ---------------------------------------------------------------------------
# Serial framing (zmk/app/src/studio/msg_framing.h)
#
# Every message is wrapped as SOF <payload> EOF. Inside the payload, any byte
# equal to SOF, ESC or EOF is sent as ESC followed by that same byte.
# ---------------------------------------------------------------------------

FRAME_START = 0xAB
FRAME_ESCAPE = 0xAC
FRAME_END = 0xAD


def encode_frame(payload: bytes) -> bytes:
    """Wrap a protobuf payload in a Studio serial frame."""
    framed = bytearray([FRAME_START])
    for byte in payload:
        if byte in (FRAME_START, FRAME_ESCAPE, FRAME_END):
            framed.append(FRAME_ESCAPE)
        framed.append(byte)
    framed.append(FRAME_END)
    return bytes(framed)


class FrameDecoder:
    """Turn a stream of serial bytes into complete frame payloads."""

    def __init__(self):
        self._buffer = bytearray()
        self._inside_frame = False
        self._escaped = False

    def feed(self, data: bytes):
        """Consume bytes and return the list of payloads completed by them."""
        completed = []
        for byte in data:
            if self._escaped:
                self._buffer.append(byte)
                self._escaped = False
            elif byte == FRAME_START:
                self._buffer.clear()
                self._inside_frame = True
            elif not self._inside_frame:
                continue  # Noise between frames.
            elif byte == FRAME_ESCAPE:
                self._escaped = True
            elif byte == FRAME_END:
                completed.append(bytes(self._buffer))
                self._buffer.clear()
                self._inside_frame = False
            else:
                self._buffer.append(byte)
        return completed


# ---------------------------------------------------------------------------
# Minimal protobuf encoding and decoding
#
# The Studio messages only use varints, zigzag varints (sint32), nested
# messages and strings, so a full protobuf library is not needed.
# ---------------------------------------------------------------------------

WIRE_VARINT = 0
WIRE_LENGTH_DELIMITED = 2


def encode_varint(value: int) -> bytes:
    encoded = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            encoded.append(byte | 0x80)
        else:
            encoded.append(byte)
            return bytes(encoded)


def encode_varint_field(field_number: int, value: int) -> bytes:
    return encode_varint((field_number << 3) | WIRE_VARINT) + encode_varint(value)


def encode_message_field(field_number: int, message: bytes) -> bytes:
    return (
        encode_varint((field_number << 3) | WIRE_LENGTH_DELIMITED)
        + encode_varint(len(message))
        + message
    )


def decode_varint(data: bytes, position: int):
    result = 0
    shift = 0
    while True:
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7


def decode_zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def decode_message(data: bytes) -> dict:
    """Decode a protobuf message into {field_number: [raw values]}.

    Varint fields become ints and length-delimited fields become bytes; the
    caller knows the schema and decodes nested messages or strings from there.
    """
    fields = {}
    position = 0
    while position < len(data):
        key, position = decode_varint(data, position)
        field_number, wire_type = key >> 3, key & 0x07
        if wire_type == WIRE_VARINT:
            value, position = decode_varint(data, position)
        elif wire_type == WIRE_LENGTH_DELIMITED:
            length, position = decode_varint(data, position)
            value = data[position:position + length]
            position += length
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")
        fields.setdefault(field_number, []).append(value)
    return fields


def first(fields: dict, field_number: int, default=None):
    values = fields.get(field_number)
    return values[0] if values else default


def decode_packed_or_repeated_varints(fields: dict, field_number: int):
    """Read a repeated uint32 field whether it was sent packed or not."""
    values = []
    for raw in fields.get(field_number, []):
        if isinstance(raw, int):
            values.append(raw)
        else:
            position = 0
            while position < len(raw):
                value, position = decode_varint(raw, position)
                values.append(value)
    return values


# ---------------------------------------------------------------------------
# Studio RPC client
#
# Field numbers below come from proto/zmk/{studio,meta,core,behaviors,
# keymap}.proto in zmk-studio-messages.
# ---------------------------------------------------------------------------

# studio.Request / studio.RequestResponse: which subsystem a message is for.
SUBSYSTEM_CORE = 3
SUBSYSTEM_BEHAVIORS = 4
SUBSYSTEM_KEYMAP = 5
RESPONSE_META = 2

# meta.ErrorConditions
META_ERROR_NAMES = {
    0: "generic error",
    1: "the keyboard is locked (press your Studio unlock key)",
    2: "request not supported by this firmware",
    3: "the keyboard could not decode the request",
    4: "the keyboard could not encode the response",
}

CORE_LOCK_STATE_UNLOCKED = 1

REQUEST_TIMEOUT_SECONDS = 5.0
UNLOCK_TIMEOUT_SECONDS = 120.0


class StudioError(Exception):
    """The keyboard answered with an error, or did not answer at all."""


def find_serial_port() -> str:
    """Return the only candidate USB serial port, or explain what to do."""
    candidates = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise StudioError(
            "No USB serial port found. Connect the keyboard with a data USB cable, "
            "make sure its output is set to USB, and close ZMK Studio if it is open."
        )
    raise StudioError(
        "Several serial ports found, choose one with --port: " + ", ".join(candidates)
    )


class StudioClient:
    """Send Studio requests over a serial port and wait for their responses."""

    def __init__(self, port: str):
        self._fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(self._fd)
        attributes = termios.tcgetattr(self._fd)
        attributes[4] = attributes[5] = termios.B115200  # Ignored by USB CDC, but must be valid.
        termios.tcsetattr(self._fd, termios.TCSANOW, attributes)
        termios.tcflush(self._fd, termios.TCIOFLUSH)
        self._decoder = FrameDecoder()
        self._pending_payloads = []
        self._next_request_id = 1

    def close(self):
        os.close(self._fd)

    def __enter__(self):
        return self

    def __exit__(self, *exception_info):
        self.close()

    def request(self, subsystem: int, subsystem_request: bytes) -> dict:
        """Send one request and return the decoded subsystem response fields."""
        request_id = self._next_request_id
        self._next_request_id += 1

        message = encode_varint_field(1, request_id) + encode_message_field(
            subsystem, subsystem_request
        )
        self._write(encode_frame(message))

        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        while True:
            payload = self._read_payload(deadline)
            response = decode_message(payload)
            request_response = first(response, 1)
            if request_response is None:
                continue  # A notification (e.g. lock state changed); not our answer.

            fields = decode_message(request_response)
            if first(fields, 1, 0) != request_id:
                continue  # Late answer to an earlier request.

            meta = first(fields, RESPONSE_META)
            if meta is not None:
                error_code = first(decode_message(meta), 2, 0)
                raise StudioError(META_ERROR_NAMES.get(error_code, f"error {error_code}"))

            subsystem_response = first(fields, subsystem)
            if subsystem_response is None:
                raise StudioError(f"Unexpected response to request {request_id}")
            return decode_message(subsystem_response)

    def _write(self, data: bytes):
        while data:
            _, writable, _ = select.select([], [self._fd], [], REQUEST_TIMEOUT_SECONDS)
            if not writable:
                raise StudioError("Timed out writing to the keyboard")
            written = os.write(self._fd, data)
            data = data[written:]

    def _read_payload(self, deadline: float) -> bytes:
        while not self._pending_payloads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StudioError(
                    "The keyboard did not answer. Is ZMK Studio enabled in its firmware, "
                    "and is ZMK Studio (the app or web page) closed?"
                )
            readable, _, _ = select.select([self._fd], [], [], remaining)
            if readable:
                data = os.read(self._fd, 4096)
                self._pending_payloads.extend(self._decoder.feed(data))
        return self._pending_payloads.pop(0)

    # -- Core --------------------------------------------------------------

    def get_device_name(self) -> str:
        response = self.request(SUBSYSTEM_CORE, encode_varint_field(1, 1))
        device_info = decode_message(first(response, 1, b""))
        return first(device_info, 1, b"").decode("utf-8", "replace")

    def is_unlocked(self) -> bool:
        response = self.request(SUBSYSTEM_CORE, encode_varint_field(2, 1))
        return first(response, 2, 0) == CORE_LOCK_STATE_UNLOCKED

    def wait_until_unlocked(self):
        if self.is_unlocked():
            return
        print("The keyboard is locked. Press the key bound to &studio_unlock "
              "(in this repo's keymap: Adjust layer, top-right key)...", flush=True)
        deadline = time.monotonic() + UNLOCK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if self.is_unlocked():
                print("Unlocked.", flush=True)
                return
        raise StudioError("Timed out waiting for the keyboard to be unlocked")

    # -- Behaviors ---------------------------------------------------------

    def list_behavior_ids(self):
        response = self.request(SUBSYSTEM_BEHAVIORS, encode_varint_field(1, 1))
        return decode_packed_or_repeated_varints(decode_message(first(response, 1, b"")), 1)

    def get_behavior_display_name(self, behavior_id: int) -> str:
        details_request = encode_message_field(2, encode_varint_field(1, behavior_id))
        response = self.request(SUBSYSTEM_BEHAVIORS, details_request)
        details = decode_message(first(response, 2, b""))
        return first(details, 2, b"").decode("utf-8", "replace")

    # -- Keymap ------------------------------------------------------------

    def get_keymap(self) -> dict:
        response = self.request(SUBSYSTEM_KEYMAP, encode_varint_field(1, 1))
        keymap = decode_message(first(response, 1, b""))
        layers = []
        for raw_layer in keymap.get(1, []):
            layer = decode_message(raw_layer)
            bindings = []
            for raw_binding in layer.get(3, []):
                binding = decode_message(raw_binding)
                bindings.append({
                    "behavior_id": decode_zigzag(first(binding, 1, 0)),
                    "param1": first(binding, 2, 0),
                    "param2": first(binding, 3, 0),
                })
            layers.append({
                "id": first(layer, 1, 0),
                "name": first(layer, 2, b"").decode("utf-8", "replace"),
                "bindings": bindings,
            })
        return {
            "layers": layers,
            "available_layers": first(keymap, 2, 0),
            "max_layer_name_length": first(keymap, 3, 0),
        }

    def get_active_physical_layout(self) -> dict:
        response = self.request(SUBSYSTEM_KEYMAP, encode_varint_field(6, 1))
        layouts = decode_message(first(response, 6, b""))
        active_index = first(layouts, 1, 0)
        layout = decode_message(layouts.get(2, [b""])[active_index])
        keys = []
        for raw_key in layout.get(2, []):
            key = decode_message(raw_key)
            keys.append({
                name: decode_zigzag(first(key, number, 0))
                for number, name in enumerate(("width", "height", "x", "y", "r", "rx", "ry"), 1)
            })
        return {"name": first(layout, 1, b"").decode("utf-8", "replace"), "keys": keys}

    # -- Keymap changes (kept in RAM until save_changes) -------------------

    def set_layer_binding(self, layer_id: int, key_position: int, behavior_id: int,
                          param1: int, param2: int) -> int:
        """Change one key. Returns 0 on success, else a SetLayerBindingResponse code."""
        binding = (encode_varint_field(1, (behavior_id << 1) ^ (behavior_id >> 31))  # sint32
                   + encode_varint_field(2, param1)
                   + encode_varint_field(3, param2))
        request = (encode_varint_field(1, layer_id)
                   + encode_varint_field(2, key_position)
                   + encode_message_field(3, binding))
        response = self.request(SUBSYSTEM_KEYMAP, encode_message_field(2, request))
        return first(response, 2, 0)

    def add_layer(self) -> int:
        """Turn one spare layer into a new layer and return its id."""
        response = self.request(SUBSYSTEM_KEYMAP, encode_message_field(9, b""))
        result = decode_message(first(response, 9, b""))
        details = first(result, 1)
        if details is None:
            raise StudioError(f"The keyboard could not add a layer (error {first(result, 2, 0)})")
        return first(decode_message(first(decode_message(details), 2, b"")), 1, 0)

    def set_layer_name(self, layer_id: int, name: str) -> int:
        request = encode_varint_field(1, layer_id) + encode_message_field(2, name.encode("utf-8"))
        response = self.request(SUBSYSTEM_KEYMAP, encode_message_field(12, request))
        return first(response, 12, 0)

    def save_changes(self):
        """Store pending changes in the keyboard's flash, like Save in ZMK Studio."""
        response = self.request(SUBSYSTEM_KEYMAP, encode_varint_field(4, 1))
        result = decode_message(first(response, 4, b""))
        if first(result, 2) is not None:
            error_code = first(result, 2)
            reasons = {1: "generic error", 2: "not supported", 3: "no space left"}
            raise StudioError(f"The keyboard could not save the changes ({reasons.get(error_code, error_code)})")


# ---------------------------------------------------------------------------
# Identifying behaviors
#
# Studio refers to behaviors by a local id that the firmware assigns and keeps
# in its settings, so the ids differ between keyboards and cannot be computed.
# What the keyboard does report is each behavior's name: its `display-name`
# property, or its device name when it has none (a custom macro, for example).
# Those names are matched to the &labels used in keymap files.
# ---------------------------------------------------------------------------

# Built-in behaviors in ZMK v0.3: reported name -> devicetree label.
BUILT_IN_BEHAVIOR_LABELS = {
    "Key Press": "kp", "Key Toggle": "kt", "Transparent": "trans", "None": "none",
    "Mod-Tap": "mt", "Layer-Tap": "lt", "Grave/Escape": "gresc",
    "Sticky Key": "sk", "Sticky Layer": "sl", "Momentary Layer": "mo",
    "Toggle Layer": "tog", "To Layer": "to", "Reset": "sys_reset",
    "Bootloader": "bootloader", "Underglow": "rgb_ug", "Bluetooth": "bt",
    "External Power": "ext_power", "Output Selection": "out", "Caps Word": "caps_word",
    "Key Repeat": "key_repeat", "Backlight": "bl", "Studio Unlock": "studio_unlock",
    "Mouse Key Press": "mkp",
    # No display-name in ZMK, so the keyboard reports the device name.
    "z_so_off": "soft_off", "mouse_move": "mmv", "mouse_scroll": "msc",
}

# Reported for a key whose behavior is not available in this firmware build.
MISSING_BEHAVIOR_ID = 0xFFFF

# What each parameter of a behavior means, used to print it readably.
BUILT_IN_BEHAVIOR_PARAMETERS = {
    "kp": ("keycode",), "kt": ("keycode",), "sk": ("keycode",),
    "mt": ("keycode", "keycode"), "lt": ("layer", "keycode"),
    "mo": ("layer",), "tog": ("layer",), "to": ("layer",), "sl": ("layer",),
    "out": ("output",), "ext_power": ("external_power",),
    "mkp": ("mouse_button",), "mmv": ("mouse_move",), "msc": ("mouse_move",),
}  # bt, rgb_ug and bl take a command plus argument and are printed separately.

# Parameter kind implied by the behaviors a custom hold-tap wraps.
HOLD_TAP_PARAMETER_KINDS = {"kp": "keycode", "mo": "layer", "tog": "layer", "to": "layer"}

def find_closing_brace(text: str, opening_brace_index: int) -> int:
    """Return the index of the '}' matching the '{' at opening_brace_index."""
    depth = 0
    for index in range(opening_brace_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("Unbalanced braces in keymap file")


def find_custom_behaviors(keymap_source: str) -> dict:
    """Find behaviors defined in the keymap: {label: (reported_names, parameter_kinds)}."""
    custom_behaviors = {}
    for match in re.finditer(r"(\w+)\s*:\s*([\w-]+)\s*\{", keymap_source):
        label, node_name = match.group(1), match.group(2)
        body = keymap_source[match.end():find_closing_brace(keymap_source, match.end() - 1)]
        compatible = re.search(r'compatible\s*=\s*"zmk,behavior-([\w-]+)"', body)
        if not compatible:
            continue

        # The keyboard reports display-name if present, else the device name,
        # which is the (deprecated) label property if present, else the node name.
        display_name = re.search(r'\bdisplay-name\s*=\s*"([^"]+)"', body)
        label_property = re.search(r'\blabel\s*=\s*"([^"]+)"', body)
        reported_names = {node_name}
        if label_property:
            reported_names.add(label_property.group(1))
        if display_name:
            reported_names.add(display_name.group(1))

        if compatible.group(1) == "hold-tap":
            wrapped = re.findall(r"<\s*&(\w+)\s*>", body)
            parameter_kinds = tuple(HOLD_TAP_PARAMETER_KINDS.get(name, "number") for name in wrapped[:2])
        else:
            cells = re.search(r"#binding-cells\s*=\s*<\s*(\d+)\s*>", body)
            parameter_kinds = ("number",) * int(cells.group(1)) if cells else ()

        custom_behaviors[label] = (reported_names, parameter_kinds)
    return custom_behaviors


class BehaviorCatalog:
    """Map the keyboard's behavior ids to devicetree labels and parameter kinds."""

    def __init__(self, reported_names_by_id: dict, keymap_source: str):
        labels_by_reported_name = dict(BUILT_IN_BEHAVIOR_LABELS)
        self._parameter_kinds_by_label = dict(BUILT_IN_BEHAVIOR_PARAMETERS)
        for label, (reported_names, parameter_kinds) in find_custom_behaviors(keymap_source).items():
            for reported_name in reported_names:
                labels_by_reported_name[reported_name] = label
            self._parameter_kinds_by_label[label] = parameter_kinds

        self._label_by_id = {
            behavior_id: labels_by_reported_name.get(name)
            for behavior_id, name in reported_names_by_id.items()
        }

    def label_for(self, behavior_id: int):
        return self._label_by_id.get(behavior_id)

    def parameter_kinds_for(self, label: str):
        return self._parameter_kinds_by_label.get(label, ())


# ---------------------------------------------------------------------------
# Printing bindings in keymap syntax
# ---------------------------------------------------------------------------

MODIFIER_FUNCTIONS = {  # Bit in the top byte of a keycode -> wrapper macro.
    0x01: "LC", 0x02: "LS", 0x04: "LA", 0x08: "LG",
    0x10: "RC", 0x20: "RS", 0x40: "RA", 0x80: "RG",
}
BLUETOOTH_COMMANDS = {0: "BT_CLR", 1: "BT_NXT", 2: "BT_PRV", 3: "BT_SEL", 4: "BT_CLR_ALL", 5: "BT_DISC"}
BLUETOOTH_COMMANDS_WITH_ARGUMENT = {"BT_SEL", "BT_DISC"}
UNDERGLOW_COMMANDS = {
    0: "RGB_TOG", 1: "RGB_ON", 2: "RGB_OFF", 3: "RGB_HUI", 4: "RGB_HUD", 5: "RGB_SAI",
    6: "RGB_SAD", 7: "RGB_BRI", 8: "RGB_BRD", 9: "RGB_SPI", 10: "RGB_SPD",
    11: "RGB_EFF", 12: "RGB_EFR", 13: "RGB_EFS",
}
UNDERGLOW_COLOR_COMMAND = 14
BACKLIGHT_COMMANDS = {0: "BL_ON", 1: "BL_OFF", 2: "BL_TOG", 3: "BL_INC", 4: "BL_DEC", 5: "BL_CYCLE", 6: "BL_SET"}
OUTPUT_TARGETS = {0: "OUT_TOG", 1: "OUT_USB", 2: "OUT_BLE"}
EXTERNAL_POWER_COMMANDS = {0: "EP_OFF", 1: "EP_ON", 2: "EP_TOG"}
MOUSE_BUTTONS = {1: "LCLK", 2: "RCLK", 4: "MCLK", 8: "MB4", 16: "MB5"}


def to_int16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


class BindingFormatter:
    """Turn raw Studio bindings into keymap text such as '&mt LCTRL ESC'."""

    def __init__(self, catalog: BehaviorCatalog, layer_names_by_id: dict, keymap_source: str):
        self._catalog = catalog
        self._layer_names_by_id = layer_names_by_id
        # Prefer the keycode spellings already used in your keymap (LSHIFT vs LSHFT...).
        self._names_in_use = set(re.findall(r"\b[A-Z][A-Z0-9_]*\b", keymap_source))
        self.warnings = []

    def format(self, binding: dict) -> str:
        behavior_id = binding["behavior_id"]
        label = self._catalog.label_for(behavior_id)
        if label is None:
            reason = ("behavior not available in this firmware" if behavior_id == MISSING_BEHAVIOR_ID
                      else f"unknown behavior id {behavior_id}")
            self.warnings.append(f"Some keys could not be identified ({reason}); "
                                 "they are marked TODO in the .keymap")
            return f"&none /* TODO: {reason}, params {binding['param1']} {binding['param2']} */"

        param1, param2 = binding["param1"], binding["param2"]
        if label == "bt":
            return "&bt " + self._command_with_argument(BLUETOOTH_COMMANDS, param1, param2,
                                                         BLUETOOTH_COMMANDS_WITH_ARGUMENT)
        if label == "bl":
            return "&bl " + self._command_with_argument(BACKLIGHT_COMMANDS, param1, param2, {"BL_SET"})
        if label == "rgb_ug":
            if param1 == UNDERGLOW_COLOR_COMMAND:
                hue, saturation, brightness = param2 >> 16, (param2 >> 8) & 0xFF, param2 & 0xFF
                return f"&rgb_ug RGB_COLOR_HSB({hue},{saturation},{brightness})"
            return "&rgb_ug " + UNDERGLOW_COMMANDS.get(param1, f"{param1} {param2}")

        parts = ["&" + label]
        for kind, value in zip(self._catalog.parameter_kinds_for(label), (param1, param2)):
            parts.append(self._format_parameter(kind, value))
        return " ".join(parts)

    @staticmethod
    def _command_with_argument(commands: dict, command: int, argument: int, takes_argument: set) -> str:
        name = commands.get(command)
        if name is None:
            return f"{command} {argument}"
        return f"{name} {argument}" if name in takes_argument else name

    def _format_parameter(self, kind: str, value: int) -> str:
        if kind == "keycode":
            return self._format_keycode(value)
        if kind == "layer":
            return self._layer_names_by_id.get(value, str(value))
        if kind == "output":
            return OUTPUT_TARGETS.get(value, str(value))
        if kind == "external_power":
            return EXTERNAL_POWER_COMMANDS.get(value, str(value))
        if kind == "mouse_button":
            return MOUSE_BUTTONS.get(value, str(value))
        if kind == "mouse_move":
            return f"MOVE({to_int16(value >> 16)}, {to_int16(value)})"
        return str(value)

    def _format_keycode(self, value: int) -> str:
        name = self._keycode_name(value)
        if name:
            return name
        # Not a named key: split off the modifiers and wrap, e.g. LC(LS(T)).
        modifiers, base = value >> 24, value & 0xFFFFFF
        base_name = self._keycode_name(base)
        if base_name is None:
            self.warnings.append(f"Unknown keycode 0x{value:08X}")
            return f"0x{value:08X}"
        for bit, function in MODIFIER_FUNCTIONS.items():
            if modifiers & bit:
                base_name = f"{function}({base_name})"
        return base_name

    def _keycode_name(self, value: int):
        aliases = KEYCODE_NAMES.get(value)
        if not aliases:
            return None
        for alias in aliases:
            if alias in self._names_in_use:
                return alias
        return aliases[0]


# ---------------------------------------------------------------------------
# Writing the backup files
# ---------------------------------------------------------------------------

def to_define_name(layer_name: str, index: int) -> str:
    name = re.sub(r"[^A-Za-z0-9]+", "_", layer_name).strip("_").upper()
    return name if name and not name[0].isdigit() else f"LAYER_{index}"


def group_keys_into_rows(physical_keys: list):
    """Split key positions into rows: consecutive keys that share the same y."""
    rows = []
    for position, key in enumerate(physical_keys):
        if rows and abs(physical_keys[rows[-1][-1]]["y"] - key["y"]) < 50:
            rows[-1].append(position)
        else:
            rows.append([position])
    return rows


def render_bindings(cells: list, physical_keys: list, cell_width: int, indent: str) -> str:
    """Lay out one layer's binding texts in rows that follow the physical board."""
    lines = []
    for row in group_keys_into_rows(physical_keys):
        line = ""
        for position in row:
            column = round(physical_keys[position]["x"] / 100)
            target = column * (cell_width + 1)
            if len(line) < target:
                line += " " * (target - len(line))
            elif line:
                line += " "
            line += cells[position].ljust(cell_width)
        lines.append(indent + line.rstrip())
    return "\n".join(lines)


def build_keymap_node(keymap: dict, physical_keys: list, formatter: BindingFormatter, indent: str) -> str:
    layer_cells = [[formatter.format(b) for b in layer["bindings"]] for layer in keymap["layers"]]
    # Size columns for normal bindings; the rare long cell (a TODO note) just overflows.
    regular_cells = [cell for cells in layer_cells for cell in cells if "/*" not in cell]
    cell_width = max(len(cell) for cell in regular_cells or [""])
    inner, body = indent + "    ", indent + "        "

    parts = [f"{indent}keymap {{", f'{inner}compatible = "zmk,keymap";', ""]
    for index, (layer, cells) in enumerate(zip(keymap["layers"], layer_cells)):
        node_name = to_define_name(layer["name"], index).lower() + "_layer"
        parts += [
            f"{inner}{node_name} {{",
            f'{body}display-name = "{layer["name"]}";',
            f"{body}bindings = <",
            render_bindings(cells, physical_keys, cell_width, body + "    "),
            f"{body}>;",
            f"{inner}}};",
            "",
        ]
    for spare in range(keymap["available_layers"]):
        parts += [f"{inner}spare_layer_{spare + 1} {{", f'{body}status = "reserved";', f"{inner}}};", ""]
    parts[-1] = f"{indent}}};"
    return "\n".join(parts)


def update_layer_defines(source: str, define_names: list) -> str:
    """Make '#define <LAYER> <index>' match the exported layer order."""
    for index, name in enumerate(define_names):
        pattern = re.compile(rf"^#define\s+{name}\s+\d+\s*$", re.M)
        if pattern.search(source):
            source = pattern.sub(f"#define {name} {index}", source)
        else:
            last_define = list(re.finditer(r"^#define\s+\w+\s+\d+\s*$", source, re.M))
            anchor = last_define[-1].end() if last_define else list(re.finditer(r"^#include.*$", source, re.M))[-1].end()
            source = source[:anchor] + f"\n#define {name} {index}" + source[anchor:]
    return source


def build_keymap_file(keymap_source: str, keymap: dict, physical_keys: list, formatter: BindingFormatter) -> str:
    """Your keymap file with only the keymap node (and layer defines) replaced."""
    match = re.search(r"^([ \t]*)keymap\s*\{", keymap_source, re.M)
    if not match:
        raise StudioError("Could not find the 'keymap {' node in the keymap file")
    end = find_closing_brace(keymap_source, match.end() - 1)
    end = keymap_source.index(";", end) + 1
    new_node = build_keymap_node(keymap, physical_keys, formatter, match.group(1))
    source = keymap_source[:match.start()] + new_node + keymap_source[end:]

    define_names = [to_define_name(layer["name"], i) for i, layer in enumerate(keymap["layers"])]
    return update_layer_defines(source, define_names)


def export_keymap(port: str, keymap_path: str, output_directory: str):
    with open(keymap_path, encoding="utf-8") as keymap_file:
        keymap_source = keymap_file.read()

    with StudioClient(port) as client:
        device_name = client.get_device_name()
        print(f"Connected to '{device_name}' on {port}", flush=True)
        client.wait_until_unlocked()

        behavior_names = {behavior_id: client.get_behavior_display_name(behavior_id)
                          for behavior_id in client.list_behavior_ids()}
        keymap = client.get_keymap()
        physical_layout = client.get_active_physical_layout()

    catalog = BehaviorCatalog(behavior_names, keymap_source)
    layer_define_names = {layer["id"]: to_define_name(layer["name"], index)
                          for index, layer in enumerate(keymap["layers"])}
    formatter = BindingFormatter(catalog, layer_define_names, keymap_source)

    keymap_text = build_keymap_file(keymap_source, keymap, physical_layout["keys"], formatter)

    backup = {
        "format_version": 1,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "device_name": device_name,
        "physical_layout": physical_layout,
        "behaviors": {
            str(behavior_id): {"label": catalog.label_for(behavior_id), "display_name": name}
            for behavior_id, name in sorted(behavior_names.items())
        },
        "keymap": {
            "available_layers": keymap["available_layers"],
            "layers": [
                {
                    "id": layer["id"],
                    "name": layer["name"],
                    "bindings": [dict(binding, keymap=formatter.format(binding))
                                 for binding in layer["bindings"]],
                }
                for layer in keymap["layers"]
            ],
        },
    }

    os.makedirs(output_directory, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base_path = os.path.join(output_directory, f"{os.path.splitext(os.path.basename(keymap_path))[0]}-{stamp}")
    with open(base_path + ".json", "w", encoding="utf-8") as json_file:
        json.dump(backup, json_file, indent=2)
        json_file.write("\n")
    with open(base_path + ".keymap", "w", encoding="utf-8") as keymap_output:
        keymap_output.write(keymap_text)

    print(f"Saved {len(keymap['layers'])} layers:")
    print(f"  {base_path}.json")
    print(f"  {base_path}.keymap")

    layer_ids = [layer["id"] for layer in keymap["layers"]]
    if layer_ids != sorted(layer_ids):
        formatter.warnings.append(
            "Layers were reordered in Studio: check that conditional layers "
            "(tri-layer) in the .keymap still point at the right layers")
    for warning in sorted(set(formatter.warnings)):
        print(f"Warning: {warning}")


SET_LAYER_BINDING_ERRORS = {1: "invalid key position", 2: "behavior rejected", 3: "invalid parameters"}


def translate_layer_parameters(binding: dict, parameter_kinds: tuple, layer_ids: dict):
    """Map layer parameters from the backup's layer ids to the keyboard's layer ids."""
    params = [binding["param1"], binding["param2"]]
    for index, kind in enumerate(parameter_kinds[:2]):
        if kind == "layer" and params[index] in layer_ids:
            params[index] = layer_ids[params[index]]
    return params


def restore_keymap(port: str, backup_path: str, keymap_path: str, dry_run: bool):
    with open(backup_path, encoding="utf-8") as backup_file:
        backup = json.load(backup_file)
    keymap_source = ""
    if keymap_path and os.path.exists(keymap_path):
        with open(keymap_path, encoding="utf-8") as keymap_file:
            keymap_source = keymap_file.read()

    backup_names = {int(behavior_id): behavior["display_name"]
                    for behavior_id, behavior in backup["behaviors"].items()}
    backup_catalog = BehaviorCatalog(backup_names, keymap_source)
    backup_layers = backup["keymap"]["layers"]

    with StudioClient(port) as client:
        device_name = client.get_device_name()
        print(f"Connected to '{device_name}' on {port}", flush=True)
        if backup.get("device_name") and device_name and backup["device_name"] != device_name:
            print(f"Warning: the backup was made on '{backup['device_name']}'", flush=True)
        client.wait_until_unlocked()

        keyboard_ids = {client.get_behavior_display_name(behavior_id): behavior_id
                        for behavior_id in client.list_behavior_ids()}
        keyboard_keymap = client.get_keymap()
        keyboard_layers = keyboard_keymap["layers"]

        key_count = len(backup["physical_layout"]["keys"])
        if keyboard_layers and len(keyboard_layers[0]["bindings"]) != key_count:
            raise StudioError(f"The backup has {key_count} keys per layer but the keyboard has "
                              f"{len(keyboard_layers[0]['bindings'])}; is it the same keyboard?")

        missing_layers = len(backup_layers) - len(keyboard_layers)
        if missing_layers > keyboard_keymap["available_layers"]:
            raise StudioError(f"The backup has {len(backup_layers)} layers but the keyboard can "
                              f"only hold {len(keyboard_layers) + keyboard_keymap['available_layers']}")

        # Layers are matched by position; extra layers are added from the spare slots.
        layer_ids = [layer["id"] for layer in keyboard_layers]
        current_bindings = [layer["bindings"] for layer in keyboard_layers]
        for _ in range(max(missing_layers, 0)):
            if dry_run:
                layer_ids.append(None)
            else:
                layer_ids.append(client.add_layer())
            current_bindings.append([None] * key_count)
        layer_id_map = {layer["id"]: layer_ids[index] for index, layer in enumerate(backup_layers)}

        changed, unchanged, failures = 0, 0, []
        for index, layer in enumerate(backup_layers):
            target_layer_id = layer_ids[index]
            if index < len(keyboard_layers) and keyboard_layers[index]["name"] != layer["name"]:
                print(f"Layer {index}: rename '{keyboard_layers[index]['name']}' -> '{layer['name']}'")
                if not dry_run:
                    client.set_layer_name(target_layer_id, layer["name"])
            elif index >= len(keyboard_layers):
                print(f"Layer {index}: add '{layer['name']}'")
                if not dry_run:
                    client.set_layer_name(target_layer_id, layer["name"])

            for position, binding in enumerate(layer["bindings"]):
                behavior_name = backup_names.get(binding["behavior_id"])
                if behavior_name not in keyboard_ids:
                    failures.append(f"{layer['name']} key {position}: '{binding['keymap']}' "
                                    "uses a behavior this firmware does not have")
                    continue

                label = backup_catalog.label_for(binding["behavior_id"])
                param1, param2 = translate_layer_parameters(
                    binding, backup_catalog.parameter_kinds_for(label), layer_id_map)
                wanted = {"behavior_id": keyboard_ids[behavior_name], "param1": param1, "param2": param2}
                if current_bindings[index][position] == wanted:
                    unchanged += 1
                    continue

                if dry_run:
                    print(f"  {layer['name']} key {position}: set {binding['keymap']}")
                    changed += 1
                    continue
                result = client.set_layer_binding(target_layer_id, position, wanted["behavior_id"],
                                                  param1, param2)
                if result == 0:
                    changed += 1
                else:
                    failures.append(f"{layer['name']} key {position}: '{binding['keymap']}' "
                                    f"({SET_LAYER_BINDING_ERRORS.get(result, result)})")

        if len(keyboard_layers) > len(backup_layers):
            print(f"Note: the keyboard has {len(keyboard_layers) - len(backup_layers)} layer(s) more "
                  "than the backup; they were left untouched")

        verb = "Would change" if dry_run else "Changed"
        print(f"{verb} {changed} key(s); {unchanged} already matched the backup")
        for failure in failures:
            print(f"{'Cannot restore' if dry_run else 'Not restored'}: {failure}")

        if dry_run:
            print("Dry run: nothing was written to the keyboard")
        elif changed or missing_layers > 0:
            client.save_changes()
            print("Saved on the keyboard")
        else:
            print("The keyboard already matches the backup; nothing to save")

    if failures and not dry_run:
        raise StudioError(f"{len(failures)} key(s) could not be restored, see above")


def find_repository_root() -> str:
    """Walk up from this script to the folder that holds config/ (the zmk-config root)."""
    directory = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.isdir(os.path.join(directory, "config")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return os.getcwd()
        directory = parent


def main():
    repository_root = find_repository_root()
    default_keymaps = sorted(glob.glob(os.path.join(repository_root, "config", "*.keymap")))

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)
    export_parser = subcommands.add_parser("export", help="save the keymap stored on the keyboard")
    export_parser.add_argument("--port", help="serial port (default: detect it)")
    export_parser.add_argument("--keymap", default=default_keymaps[0] if default_keymaps else None,
                               help="keymap file used as template (default: config/*.keymap)")
    export_parser.add_argument("--output-dir", default=os.path.join(repository_root, "keymap-backups"),
                               help="where to write the backup (default: keymap-backups/)")

    restore_parser = subcommands.add_parser("restore", help="write a JSON backup back to the keyboard")
    restore_parser.add_argument("backup", help="backup file written by 'export' (.json)")
    restore_parser.add_argument("--port", help="serial port (default: detect it)")
    restore_parser.add_argument("--keymap", default=default_keymaps[0] if default_keymaps else None,
                                help="keymap file defining your custom behaviors (default: config/*.keymap)")
    restore_parser.add_argument("--dry-run", action="store_true",
                                help="only show what would change, write nothing")
    arguments = parser.parse_args()

    try:
        port = arguments.port or find_serial_port()
        if arguments.command == "export":
            if not arguments.keymap:
                raise StudioError("No keymap file found in config/; pass one with --keymap")
            export_keymap(port, arguments.keymap, arguments.output_dir)
        else:
            restore_keymap(port, arguments.backup, arguments.keymap, arguments.dry_run)
    except (StudioError, OSError, ValueError, KeyError) as error:
        sys.exit(f"Error: {error}")




# ---------------------------------------------------------------------------
# Generated data: keycode value -> ZMK names (first one is the preferred
# spelling). Built with the C preprocessor from ZMK v0.3
# include/dt-bindings/zmk/keys.h, so every alias there is covered.
# ---------------------------------------------------------------------------

KEYCODE_NAMES = {
    0x00010081: ("SYS_PWR", "SYSTEM_POWER",),
    0x00010082: ("SYS_SLEEP", "SYSTEM_SLEEP",),
    0x00010083: ("SYS_WAKE", "SYSTEM_WAKE_UP",),
    0x00070004: ("A",),
    0x00070005: ("B",),
    0x00070006: ("C",),
    0x00070007: ("D",),
    0x00070008: ("E",),
    0x00070009: ("F",),
    0x0007000A: ("G",),
    0x0007000B: ("H",),
    0x0007000C: ("I",),
    0x0007000D: ("J",),
    0x0007000E: ("K",),
    0x0007000F: ("L",),
    0x00070010: ("M",),
    0x00070011: ("N",),
    0x00070012: ("O",),
    0x00070013: ("P",),
    0x00070014: ("Q",),
    0x00070015: ("R",),
    0x00070016: ("S",),
    0x00070017: ("T",),
    0x00070018: ("U",),
    0x00070019: ("V",),
    0x0007001A: ("W",),
    0x0007001B: ("X",),
    0x0007001C: ("Y",),
    0x0007001D: ("Z",),
    0x0007001E: ("N1", "NUM_1", "NUMBER_1",),
    0x0007001F: ("N2", "NUM_2", "NUMBER_2",),
    0x00070020: ("N3", "NUM_3", "NUMBER_3",),
    0x00070021: ("N4", "NUM_4", "NUMBER_4",),
    0x00070022: ("N5", "NUM_5", "NUMBER_5",),
    0x00070023: ("N6", "NUM_6", "NUMBER_6",),
    0x00070024: ("N7", "NUM_7", "NUMBER_7",),
    0x00070025: ("N8", "NUM_8", "NUMBER_8",),
    0x00070026: ("N9", "NUM_9", "NUMBER_9",),
    0x00070027: ("N0", "NUM_0", "NUMBER_0",),
    0x00070028: ("RET", "ENTER", "RETURN",),
    0x00070029: ("ESC", "ESCAPE",),
    0x0007002A: ("BSPC", "BKSP", "BACKSPACE",),
    0x0007002B: ("TAB",),
    0x0007002C: ("SPACE", "SPC",),
    0x0007002D: ("MINUS",),
    0x0007002E: ("EQUAL", "EQL",),
    0x0007002F: ("LBKT",),
    0x00070030: ("RBKT",),
    0x00070031: ("BSLH", "BACKSLASH",),
    0x00070032: ("NUHS", "NON_US_HASH",),
    0x00070033: ("SEMI", "SCLN", "SEMICOLON",),
    0x00070034: ("SQT", "APOS", "QUOT", "APOSTROPHE", "SINGLE_QUOTE",),
    0x00070035: ("GRAVE", "GRAV",),
    0x00070036: ("COMMA", "CMMA",),
    0x00070037: ("DOT", "PERIOD",),
    0x00070038: ("FSLH", "SLASH",),
    0x00070039: ("CAPS", "CLCK", "CAPSLOCK",),
    0x0007003A: ("F1",),
    0x0007003B: ("F2",),
    0x0007003C: ("F3",),
    0x0007003D: ("F4",),
    0x0007003E: ("F5",),
    0x0007003F: ("F6",),
    0x00070040: ("F7",),
    0x00070041: ("F8",),
    0x00070042: ("F9",),
    0x00070043: ("F10",),
    0x00070044: ("F11",),
    0x00070045: ("F12",),
    0x00070046: ("PSCRN", "PRSC", "PRINTSCREEN",),
    0x00070047: ("SCLK", "SLCK", "SCROLLLOCK",),
    0x00070048: ("PAUS", "PAUSE_BREAK",),
    0x00070049: ("INS", "INSERT",),
    0x0007004A: ("HOME",),
    0x0007004B: ("PG_UP", "PGUP", "PAGE_UP",),
    0x0007004C: ("DEL", "DELETE",),
    0x0007004D: ("END",),
    0x0007004E: ("PG_DN", "PGDN", "PAGE_DOWN",),
    0x0007004F: ("RIGHT", "RARW", "RIGHT_ARROW",),
    0x00070050: ("LEFT", "LARW", "LEFT_ARROW",),
    0x00070051: ("DOWN", "DARW", "DOWN_ARROW",),
    0x00070052: ("UP", "UARW", "UP_ARROW",),
    0x00070053: ("KP_NUM", "KP_NLCK", "KP_NUMLOCK",),
    0x00070054: ("KDIV", "KP_SLASH", "KP_DIVIDE",),
    0x00070055: ("KMLT", "KP_ASTERISK", "KP_MULTIPLY",),
    0x00070056: ("KMIN", "KP_MINUS", "KP_SUBTRACT",),
    0x00070057: ("KPLS", "KP_PLUS",),
    0x00070058: ("KP_ENTER",),
    0x00070059: ("KP_N1", "KP_NUMBER_1",),
    0x0007005A: ("KP_N2", "KP_NUMBER_2",),
    0x0007005B: ("KP_N3", "KP_NUMBER_3",),
    0x0007005C: ("KP_N4", "KP_NUMBER_4",),
    0x0007005D: ("KP_N5", "KP_NUMBER_5",),
    0x0007005E: ("KP_N6", "KP_NUMBER_6",),
    0x0007005F: ("KP_N7", "KP_NUMBER_7",),
    0x00070060: ("KP_N8", "KP_NUMBER_8",),
    0x00070061: ("KP_N9", "KP_NUMBER_9",),
    0x00070062: ("KP_N0", "KP_NUMBER_0",),
    0x00070063: ("KP_DOT",),
    0x00070064: ("NUBS", "NON_US_BSLH",),
    0x00070065: ("K_CMENU", "GUI", "K_APP", "K_APPLICATION", "K_CONTEXT_MENU",),
    0x00070066: ("K_PWR", "K_POWER",),
    0x00070067: ("KP_EQUAL",),
    0x00070068: ("F13",),
    0x00070069: ("F14",),
    0x0007006A: ("F15",),
    0x0007006B: ("F16",),
    0x0007006C: ("F17",),
    0x0007006D: ("F18",),
    0x0007006E: ("F19",),
    0x0007006F: ("F20",),
    0x00070070: ("F21",),
    0x00070071: ("F22",),
    0x00070072: ("F23",),
    0x00070073: ("F24",),
    0x00070074: ("K_EXEC", "K_EXECUTE",),
    0x00070075: ("K_HELP",),
    0x00070076: ("K_MENU",),
    0x00070077: ("K_SELECT",),
    0x00070078: ("K_STOP",),
    0x00070079: ("K_REDO", "K_AGAIN",),
    0x0007007A: ("UNDO", "K_UNDO",),
    0x0007007B: ("CUT", "K_CUT",),
    0x0007007C: ("COPY", "K_COPY",),
    0x0007007D: ("PSTE", "K_PASTE",),
    0x0007007E: ("K_FIND",),
    0x0007007F: ("K_MUTE",),
    0x00070080: ("VOLU", "K_VOL_UP", "K_VOLUME_UP",),
    0x00070081: ("VOLD", "K_VOL_DN", "K_VOLUME_DOWN",),
    0x00070082: ("LCAPS", "LOCKING_CAPS",),
    0x00070083: ("LNLCK", "LOCKING_NUM",),
    0x00070084: ("LSLCK", "LOCKING_SCROLL",),
    0x00070085: ("KP_COMMA",),
    0x00070086: ("KP_EQUAL_AS400",),
    0x00070087: ("INT1", "INT_RO", "INTERNATIONAL_1",),
    0x00070088: ("INT2", "INT_KANA", "INTERNATIONAL_2", "INT_KATAKANAHIRAGANA",),
    0x00070089: ("INT3", "INT_YEN", "INTERNATIONAL_3",),
    0x0007008A: ("INT4", "INT_HENKAN", "INTERNATIONAL_4",),
    0x0007008B: ("INT5", "INT_MUHENKAN", "INTERNATIONAL_5",),
    0x0007008C: ("INT6", "INT_KPJPCOMMA", "INTERNATIONAL_6",),
    0x0007008D: ("INT7", "INTERNATIONAL_7",),
    0x0007008E: ("INT8", "INTERNATIONAL_8",),
    0x0007008F: ("INT9", "INTERNATIONAL_9",),
    0x00070090: ("LANG1", "LANGUAGE_1", "LANG_HANGEUL",),
    0x00070091: ("LANG2", "LANGUAGE_2", "LANG_HANJA",),
    0x00070092: ("LANG3", "LANGUAGE_3", "LANG_KATAKANA",),
    0x00070093: ("LANG4", "LANGUAGE_4", "LANG_HIRAGANA",),
    0x00070094: ("LANG5", "LANGUAGE_5", "LANG_ZENKAKUHANKAKU",),
    0x00070095: ("LANG6", "LANGUAGE_6",),
    0x00070096: ("LANG7", "LANGUAGE_7",),
    0x00070097: ("LANG8", "LANGUAGE_8",),
    0x00070098: ("LANG9", "LANGUAGE_9",),
    0x00070099: ("ALT_ERASE",),
    0x0007009A: ("SYSREQ", "ATTENTION",),
    0x0007009B: ("K_CANCEL",),
    0x0007009C: ("CLEAR",),
    0x0007009D: ("PRIOR",),
    0x0007009E: ("RET2", "RETURN2",),
    0x0007009F: ("SEPARATOR",),
    0x000700A0: ("OUT",),
    0x000700A1: ("OPER",),
    0x000700A2: ("CLEAR_AGAIN",),
    0x000700A3: ("CRSEL",),
    0x000700A4: ("EXSEL",),
    0x000700B6: ("KP_LPAR", "KP_LEFT_PARENTHESIS",),
    0x000700B7: ("KP_RPAR", "KP_RIGHT_PARENTHESIS",),
    0x000700D8: ("KP_CLEAR",),
    0x000700E0: ("LCTRL", "LCTL", "LEFT_CONTROL",),
    0x000700E1: ("LSHFT", "LSFT", "LSHIFT", "LEFT_SHIFT",),
    0x000700E2: ("LALT", "LEFT_ALT",),
    0x000700E3: ("LGUI", "LCMD", "LWIN", "LMETA", "LEFT_GUI", "LEFT_WIN", "LEFT_META", "LEFT_COMMAND",),
    0x000700E4: ("RCTRL", "RCTL", "RIGHT_CONTROL",),
    0x000700E5: ("RSHFT", "RSFT", "RSHIFT", "RIGHT_SHIFT",),
    0x000700E6: ("RALT", "RIGHT_ALT",),
    0x000700E7: ("RGUI", "RCMD", "RWIN", "RMETA", "RIGHT_GUI", "RIGHT_WIN", "RIGHT_META", "RIGHT_COMMAND",),
    0x000700E8: ("K_PP", "K_PLAY_PAUSE",),
    0x000700E9: ("K_STOP2",),
    0x000700EA: ("K_PREV", "K_PREVIOUS",),
    0x000700EB: ("K_NEXT",),
    0x000700EC: ("K_EJECT",),
    0x000700ED: ("K_VOL_UP2", "K_VOLUME_UP2",),
    0x000700EE: ("K_VOL_DN2", "K_VOLUME_DOWN2",),
    0x000700EF: ("K_MUTE2",),
    0x000700F0: ("K_WWW",),
    0x000700F1: ("K_BACK",),
    0x000700F2: ("K_FORWARD",),
    0x000700F3: ("K_STOP3",),
    0x000700F4: ("K_FIND2",),
    0x000700F5: ("K_SCROLL_UP",),
    0x000700F6: ("K_SCROLL_DOWN",),
    0x000700F7: ("K_EDIT",),
    0x000700F8: ("K_SLEEP",),
    0x000700F9: ("K_LOCK", "K_COFFEE", "K_SCREENSAVER",),
    0x000700FA: ("K_REFRESH",),
    0x000700FB: ("K_CALC", "K_CALCULATOR",),
    0x000C0030: ("C_PWR", "C_POWER",),
    0x000C0031: ("C_RESET",),
    0x000C0032: ("C_SLEEP",),
    0x000C0034: ("C_SLEEP_MODE",),
    0x000C0040: ("C_MENU",),
    0x000C0041: ("C_MENU_PICK", "C_MENU_SELECT",),
    0x000C0042: ("C_MENU_UP",),
    0x000C0043: ("C_MENU_DOWN",),
    0x000C0044: ("C_MENU_LEFT",),
    0x000C0045: ("C_MENU_RIGHT",),
    0x000C0046: ("C_MENU_ESC", "C_MENU_ESCAPE",),
    0x000C0047: ("C_MENU_INC", "C_MENU_INCREASE",),
    0x000C0048: ("C_MENU_DEC", "C_MENU_DECREASE",),
    0x000C0060: ("C_DATA_ON_SCREEN",),
    0x000C0061: ("C_CAPTIONS", "C_SUBTITLES",),
    0x000C0065: ("C_SNAPSHOT",),
    0x000C0067: ("C_PIP",),
    0x000C0069: ("C_RED", "C_RED_BUTTON",),
    0x000C006A: ("C_GREEN", "C_GREEN_BUTTON",),
    0x000C006B: ("C_BLUE", "C_BLUE_BUTTON",),
    0x000C006C: ("C_YELLOW", "C_YELLOW_BUTTON",),
    0x000C006D: ("C_ASPECT",),
    0x000C006F: ("C_BRI_UP", "C_BRI_INC",),
    0x000C0070: ("C_BRI_DN", "C_BRI_DEC",),
    0x000C0072: ("C_BKLT_TOG",),
    0x000C0073: ("C_BRI_MIN",),
    0x000C0074: ("C_BRI_MAX",),
    0x000C0075: ("C_BRI_AUTO",),
    0x000C0082: ("C_MODE_STEP", "C_MEDIA_STEP",),
    0x000C0083: ("C_CHAN_LAST", "C_RECALL_LAST",),
    0x000C0089: ("C_MEDIA_TV",),
    0x000C008A: ("C_MEDIA_WWW",),
    0x000C008B: ("C_MEDIA_DVD",),
    0x000C008C: ("C_MEDIA_PHONE",),
    0x000C008F: ("C_MEDIA_GAMES",),
    0x000C0091: ("C_MEDIA_CD",),
    0x000C0092: ("C_MEDIA_VCR",),
    0x000C0093: ("C_MEDIA_TUNER",),
    0x000C0094: ("C_QUIT",),
    0x000C0095: ("C_HELP",),
    0x000C0096: ("C_MEDIA_TAPE",),
    0x000C0097: ("C_MEDIA_CABLE",),
    0x000C009A: ("C_MEDIA_HOME",),
    0x000C009C: ("C_CHAN_INC", "C_CHANNEL_INC",),
    0x000C009D: ("C_CHAN_DEC", "C_CHANNEL_DEC",),
    0x000C00A0: ("C_MEDIA_VCR_PLUS",),
    0x000C00B0: ("C_PLAY",),
    0x000C00B1: ("C_PAUSE",),
    0x000C00B2: ("C_REC", "C_RECORD",),
    0x000C00B3: ("C_FF", "C_FAST_FORWARD",),
    0x000C00B4: ("C_RW", "C_REWIND",),
    0x000C00B5: ("C_NEXT", "M_NEXT",),
    0x000C00B6: ("C_PREV", "M_PREV", "C_PREVIOUS",),
    0x000C00B7: ("C_STOP", "M_STOP",),
    0x000C00B8: ("M_EJCT", "C_EJECT",),
    0x000C00B9: ("C_SHUFFLE", "C_RANDOM_PLAY",),
    0x000C00BC: ("C_REPEAT",),
    0x000C00BF: ("C_SLOW2", "C_SLOW_TRACKING",),
    0x000C00CC: ("C_STOP_EJECT",),
    0x000C00CD: ("C_PP", "M_PLAY", "C_PLAY_PAUSE",),
    0x000C00CF: ("C_VOICE_COMMAND",),
    0x000C00E2: ("C_MUTE", "M_MUTE",),
    0x000C00E5: ("C_BASS_BOOST",),
    0x000C00E9: ("C_VOL_UP", "M_VOLU", "C_VOLUME_UP",),
    0x000C00EA: ("C_VOL_DN", "M_VOLD", "C_VOLUME_DOWN",),
    0x000C00F5: ("C_SLOW",),
    0x000C0173: ("C_ALT_AUDIO_INC",),
    0x000C0184: ("C_AL_WORD",),
    0x000C0185: ("C_AL_TEXT_EDITOR",),
    0x000C0186: ("C_AL_SHEET", "C_AL_SPREADSHEET",),
    0x000C0189: ("C_AL_DB", "C_AL_DATABASE",),
    0x000C018A: ("C_AL_MAIL", "C_AL_EMAIL",),
    0x000C018B: ("C_AL_NEWS",),
    0x000C018C: ("C_AL_VOICEMAIL",),
    0x000C018D: ("C_AL_ADDRESS_BOOK",),
    0x000C018E: ("C_AL_CAL", "C_AL_CALENDAR",),
    0x000C0190: ("C_AL_JOURNAL",),
    0x000C0191: ("C_AL_FINANCE",),
    0x000C0192: ("C_AL_CALC", "C_AL_CALCULATOR",),
    0x000C0196: ("C_AL_WWW",),
    0x000C0199: ("C_AL_CHAT", "C_AL_NETWORK_CHAT",),
    0x000C019C: ("C_AL_LOGOFF",),
    0x000C019E: ("C_AL_COFFEE", "C_AL_SCREENSAVER",),
    0x000C019F: ("C_AL_CONTROL_PANEL",),
    0x000C01A4: ("C_AL_PREV_TASK",),
    0x000C01A6: ("C_AL_HELP",),
    0x000C01A7: ("C_AL_DOCS", "C_AL_DOCUMENTS",),
    0x000C01AB: ("C_AL_SPELL", "C_AL_SPELLCHECK",),
    0x000C01B1: ("C_AL_SCREEN_SAVER",),
    0x000C01B4: ("C_AL_FILES", "C_AL_FILE_BROWSER",),
    0x000C01B6: ("C_AL_IMAGES", "C_AL_IMAGE_BROWSER",),
    0x000C01B7: ("C_AL_AUDIO", "C_AL_MUSIC", "C_AL_AUDIO_BROWSER",),
    0x000C01B8: ("C_AL_MOVIES", "C_AL_MOVIE_BROWSER",),
    0x000C01BC: ("C_AL_IM",),
    0x000C01BD: ("C_AL_TIPS", "C_AL_TUTORIAL",),
    0x000C0201: ("C_AC_NEW",),
    0x000C0202: ("C_AC_OPEN",),
    0x000C0203: ("C_AC_CLOSE",),
    0x000C0204: ("C_AC_EXIT",),
    0x000C0207: ("C_AC_SAVE",),
    0x000C0208: ("C_AC_PRINT",),
    0x000C0209: ("C_AC_PROPS", "C_AC_PROPERTIES",),
    0x000C021A: ("C_AC_UNDO",),
    0x000C021B: ("C_AC_COPY",),
    0x000C021C: ("C_AC_CUT",),
    0x000C021D: ("C_AC_PASTE",),
    0x000C021F: ("C_AC_FIND",),
    0x000C0221: ("C_AC_SEARCH",),
    0x000C0222: ("C_AC_GOTO",),
    0x000C0223: ("C_AC_HOME",),
    0x000C0224: ("C_AC_BACK",),
    0x000C0225: ("C_AC_FORWARD",),
    0x000C0226: ("C_AC_STOP",),
    0x000C0227: ("C_AC_REFRESH",),
    0x000C022A: ("C_AC_BOOKMARKS", "C_AC_FAVORITES", "C_AC_FAVOURITES",),
    0x000C022D: ("C_AC_ZOOM_IN",),
    0x000C022E: ("C_AC_ZOOM_OUT",),
    0x000C022F: ("C_AC_ZOOM",),
    0x000C0232: ("C_AC_VIEW_TOGGLE",),
    0x000C0233: ("C_AC_SCROLL_UP",),
    0x000C0234: ("C_AC_SCROLL_DOWN",),
    0x000C023D: ("C_AC_EDIT",),
    0x000C025F: ("C_AC_CANCEL",),
    0x000C0269: ("C_AC_INS", "C_AC_INSERT",),
    0x000C026A: ("C_AC_DEL",),
    0x000C0279: ("C_AC_REDO",),
    0x000C0289: ("C_AC_REPLY",),
    0x000C028B: ("C_AC_FORWARD_MAIL",),
    0x000C028C: ("C_AC_SEND",),
    0x000C029D: ("GLOBE", "C_AC_NEXT_KEYBOARD_LAYOUT_SELECT",),
    0x000C02C7: ("C_KBIA_PREV",),
    0x000C02C8: ("C_KBIA_NEXT",),
    0x000C02C9: ("C_KBIA_PREV_GRP",),
    0x000C02CA: ("C_KBIA_NEXT_GRP",),
    0x000C02CB: ("C_KBIA_ACCEPT",),
    0x000C02CC: ("C_KBIA_CANCEL",),
    0x0207001E: ("EXCL", "BANG", "EXCLAMATION",),
    0x0207001F: ("AT", "ATSN", "AT_SIGN",),
    0x02070020: ("HASH", "POUND",),
    0x02070021: ("DLLR", "DOLLAR",),
    0x02070022: ("PRCNT", "PRCT", "PERCENT",),
    0x02070023: ("CARET", "CRRT",),
    0x02070024: ("AMPS", "AMPERSAND",),
    0x02070025: ("STAR", "ASTRK", "ASTERISK",),
    0x02070026: ("LPAR", "LPRN",),
    0x02070027: ("RPAR", "RPRN",),
    0x0207002D: ("UNDER", "UNDERSCORE",),
    0x0207002E: ("PLUS",),
    0x0207002F: ("LBRC", "LCUR",),
    0x02070030: ("RBRC", "RCUR",),
    0x02070031: ("PIPE",),
    0x02070032: ("TILDE2",),
    0x02070033: ("COLON", "COLN",),
    0x02070034: ("DQT",),
    0x02070035: ("TILDE", "TILD",),
    0x02070036: ("LT", "LABT", "LESS_THAN",),
    0x02070037: ("GT", "RABT",),
    0x02070038: ("QMARK", "QUESTION",),
    0x02070053: ("CLEAR2",),
    0x02070064: ("PIPE2",),
}


if __name__ == "__main__":
    main()
