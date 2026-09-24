# Studio keymap backup

`studio_keymap.py` saves the keymap that is stored **on the keyboard**, including
every change made with [ZMK Studio](https://zmk.studio), into files you can keep
in Git.

ZMK Studio edits the keymap live and stores it in the keyboard's flash, but it has
no export button. If the keyboard's settings are ever wiped, or you move to a new
controller, those changes are gone. This script reads them back over USB, using
the same protocol as ZMK Studio, and turns them into:

- an exact **JSON** copy of every key, and
- a complete, buildable **`.keymap`** file you can drop into `config/`.

## Requirements

- macOS or Linux, with **Python 3.8 or newer**. Only the standard library is used:
  there is nothing to install with `pip`.
- The keyboard connected with a **USB data cable** (not a charge-only cable), with
  its output set to USB (Adjust layer: `Esc` toggles USB / Bluetooth).
- Firmware built with ZMK Studio enabled, as the firmware from this repository is.
- **ZMK Studio closed.** Only one program can use the keyboard's serial port at a
  time.

## Quick start

From the repository root:

```sh
python3 scripts/studio-keymap/studio_keymap.py export
```

If the keyboard is locked, the script asks you to unlock it and waits for up to
two minutes. On this keyboard the unlock key is **LOWER + RAISE + the top-right
key** (the `&studio_unlock` binding on the Adjust layer).

Example output:

```text
Connected to 'Rev57LP' on /dev/cu.usbmodem1101
The keyboard is locked. Press your Studio unlock key (on this keyboard: LOWER + RAISE + top-right key)...
Unlocked.
Saved 4 layers:
  /Users/you/zmk-config/keymap-backups/rev57lp-20260924-201257.json
  /Users/you/zmk-config/keymap-backups/rev57lp-20260924-201257.keymap
```

## Output files

Both files are written to `keymap-backups/` at the repository root and named after
the keymap and the time of the export, so older backups are never overwritten.

### `<keymap>-<date>-<time>.keymap`

Your current `config/rev57lp.keymap` with only two things replaced:

- the `keymap { ... }` node, rebuilt from the layers read from the keyboard, in the
  order they have on the keyboard, with their names and any spare
  (`status = "reserved"`) layers Studio still has available;
- the `#define <LAYER> <index>` lines, so names such as `LOWER` and `RAISE` match
  the exported layer order.

Everything else (includes, macros, custom behaviors, conditional layers, combos)
is copied unchanged. The bindings are laid out in rows and columns that follow the
physical shape of the board, which makes them easy to read and to compare.

Keycodes are written with the same spelling your keymap already uses (for example
`LSHIFT` rather than `LSHFT`).

### `<keymap>-<date>-<time>.json`

The raw data, for tooling and for a future restore:

| Field | Content |
|---|---|
| `device_name` | Name reported by the keyboard |
| `physical_layout` | Position and size of every key |
| `behaviors` | Behavior ids on this keyboard, with their name and `&label` |
| `keymap.layers[]` | For each layer: `id`, `name` and `bindings[]` |
| `bindings[]` | `behavior_id`, `param1`, `param2` exactly as stored, plus `keymap`, the same binding as keymap text |
| `keymap.available_layers` | Spare layer slots Studio can still add |

## Using a backup

### Make it the firmware's default keymap

This is the recovery path: after it, any freshly flashed or reset keyboard starts
with your layout.

1. Compare the export with the current file:

   ```sh
   diff config/rev57lp.keymap keymap-backups/rev57lp-<date>-<time>.keymap
   ```

2. If it looks right, copy it over, commit and push:

   ```sh
   cp keymap-backups/rev57lp-<date>-<time>.keymap config/rev57lp.keymap
   git add config/rev57lp.keymap keymap-backups/
   git commit -m "Update keymap from ZMK Studio"
   git push
   ```

3. GitHub Actions builds the new firmware. Flash it as usual.

Flashing new firmware does **not** erase the changes stored by Studio: the keyboard
keeps using them. To make the keyboard use the keymap compiled into the firmware,
use **Restore Stock Settings** in ZMK Studio. If the backup went into `config/`
first, nothing changes except that the stored copy and the compiled one are now
the same.

### Keep it only as a record

Commit the files in `keymap-backups/` without touching `config/`. The JSON keeps
the exact values and the `.keymap` is readable history of how the layout evolved.

## Options

```text
python3 scripts/studio-keymap/studio_keymap.py export [--port PORT] [--keymap FILE] [--output-dir DIR]
```

| Option | Default | Use it when |
|---|---|---|
| `--port` | Detected: the only `/dev/cu.usbmodem*` (macOS) or `/dev/ttyACM*` (Linux) | Several serial devices are connected |
| `--keymap` | The `.keymap` file in `config/` | You want another file as template |
| `--output-dir` | `keymap-backups/` | You want the files somewhere else |

To find the port by hand on macOS, run `ls /dev/cu.usbmodem*` with the keyboard
unplugged and again with it plugged in.

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `No USB serial port found` | The keyboard is not connected over USB, the cable is charge-only, or the output is on Bluetooth. |
| `Several serial ports found` | Pass the right one with `--port`. |
| `Resource busy` or `The keyboard did not answer` | ZMK Studio (browser tab or app) or another program still has the port open: close it and try again. If it persists, the firmware may have been built without ZMK Studio. |
| `Timed out waiting for the keyboard to be unlocked` | The unlock key was not pressed within two minutes. Run the script again. |
| `Some keys could not be identified (behavior not available in this firmware)` | Those keys point to a behavior the firmware does not include. They are exported as `&none` with a `/* TODO */` note holding the raw values. |
| `Some keys could not be identified (unknown behavior id N)` | The keyboard has a behavior the script does not recognise, for example a custom one that is not defined in the keymap file used as template. Same `/* TODO */` handling. |
| `Layers were reordered in Studio` | Conditional layers (the LOWER + RAISE = Adjust rule) refer to layers by number. Check them in the exported `.keymap`. |

Unknown keys are never guessed: a `/* TODO */` note keeps the exact data so it can
be fixed by hand.

## How it works

1. **Serial connection.** The script opens the keyboard's USB serial port in raw
   mode. Studio messages are framed as `0xAB <payload> 0xAD`; any of those bytes
   (or the escape byte `0xAC`) inside the payload is preceded by `0xAC`.
2. **Protocol.** Payloads are protobuf messages from
   [zmk-studio-messages](https://github.com/zmkfirmware/zmk-studio-messages). The
   script encodes and decodes the few message types it needs by hand, which is why
   it needs no dependencies. It sends these requests:
   - `core.get_device_info` and `core.get_lock_state` (waits while locked);
   - `behaviors.list_all_behaviors` and `behaviors.get_behavior_details`;
   - `keymap.get_keymap` and `keymap.get_physical_layouts`.
3. **Behaviors.** The keyboard refers to behaviors by numeric ids that it assigns
   and keeps in its own settings, so the same behavior can have a different id on
   another keyboard. The script therefore asks the keyboard for each behavior's
   name (its `display-name`, or its devicetree name when it has none) and maps
   that name to a `&label`:
   - built-in ZMK behaviors (`Key Press` → `&kp`, `Mod-Tap` → `&mt`, ...);
   - custom behaviors found in the template keymap (macros, hold-taps), matched by
     node name, `label` or `display-name`.
4. **Parameters.** Each parameter is printed according to what it means for that
   behavior: a keycode (`LCTRL`, `LS(N1)`), a layer (`RAISE`), a Bluetooth command
   (`BT_SEL 2`), an underglow command, an output, and so on.
5. **Keycode names.** The table at the end of the script maps every keycode value
   to all its ZMK names. It was generated with the C preprocessor from ZMK v0.3
   `include/dt-bindings/zmk/keys.h`, so the values are exact.

## Limitations

- Only layers and bindings are exported: that is all ZMK Studio can edit. Macros,
  combos and behavior settings (such as tapping terms) always come from the keymap
  file.
- Written for ZMK v0.3. A later ZMK version may add behaviors or keycodes; they
  show up as `/* TODO */` until the tables in the script are updated.
- Windows is not supported (the script uses POSIX serial port calls).

## Testing

The script was tested end to end against ZMK v0.3 compiled as a `native_posix_64`
simulation with the Rev57LP physical layout and Studio on a virtual serial port:
lock and unlock, a binding changed through the Studio protocol, export, a new
firmware built from the exported `.keymap`, and a second export that matched the
first.

## Planned

A `restore` command that writes a JSON backup back to the keyboard through Studio,
so a layout can be recovered without rebuilding the firmware.
