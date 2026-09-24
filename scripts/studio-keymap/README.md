# Studio keymap backup and restore

`studio_keymap.py` saves the keymap that is stored **on the keyboard**, including
every change made with [ZMK Studio](https://zmk.studio), into files you can keep
in Git, and writes a saved keymap back to the keyboard.

ZMK Studio edits the keymap live and stores it in the keyboard's flash, but it has
no export or import. If the keyboard's settings are ever wiped, or you move to a
new controller, those changes are gone. This script uses the same protocol as ZMK
Studio, over USB, to:

- **export** the keymap as an exact **JSON** copy of every key plus a complete,
  buildable **`.keymap`** file you can drop into `config/`;
- **restore** a JSON backup onto the keyboard, without rebuilding the firmware.

## Requirements

- macOS or Linux, with **Python 3.8 or newer**. Only the standard library is used:
  there is nothing to install with `pip`.
- The keyboard connected with a **USB data cable** (not a charge-only cable), with
  its output set to USB (Adjust layer: `Esc` toggles USB / Bluetooth).
- Firmware built with ZMK Studio enabled, as the firmware from this repository is.
- **ZMK Studio closed.** Only one program can use the keyboard's serial port at a
  time.

Both commands need the keyboard **unlocked**. If it is locked, the script asks you
to press the key bound to `&studio_unlock` and waits for up to two minutes. In this
repository's keymap that key is the **top-right key of the Adjust layer**.

## Export

From the repository root:

```sh
python3 scripts/studio-keymap/studio_keymap.py export
```

```text
Connected to 'Rev57LP' on /dev/cu.usbmodem1101
The keyboard is locked. Press the key bound to &studio_unlock (in this repo's keymap: Adjust layer, top-right key)...
Unlocked.
Saved 4 layers:
  /Users/you/zmk-config/keymap-backups/rev57lp-20260924-223813.json
  /Users/you/zmk-config/keymap-backups/rev57lp-20260924-223813.keymap
```

Both files are written to `keymap-backups/` at the repository root and named after
the keymap and the time of the export, so older backups are never overwritten.
Commit them to keep them:

```sh
git add keymap-backups/
git commit -m "Back up keymap from ZMK Studio"
```

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
Keycodes are written with the spelling your keymap already uses (for example
`LSHIFT` rather than `LSHFT`).

### `<keymap>-<date>-<time>.json`

The raw data, used by `restore`:

| Field | Content |
|---|---|
| `device_name` | Name reported by the keyboard |
| `physical_layout` | Position and size of every key |
| `behaviors` | Behavior ids on this keyboard, with their name and `&label` |
| `keymap.layers[]` | For each layer: `id`, `name` and `bindings[]` |
| `bindings[]` | `behavior_id`, `param1`, `param2` exactly as stored, plus `keymap`, the same binding as keymap text |
| `keymap.available_layers` | Spare layer slots Studio can still add |

## Restore

There are two ways to get a saved keymap back onto a keyboard.

### From the JSON, without rebuilding

Check first what would change (nothing is written):

```sh
python3 scripts/studio-keymap/studio_keymap.py restore keymap-backups/rev57lp-<date>-<time>.json --dry-run
```

Then restore it:

```sh
python3 scripts/studio-keymap/studio_keymap.py restore keymap-backups/rev57lp-<date>-<time>.json
```

```text
Connected to 'Rev57LP' on /dev/cu.usbmodem1101
Changed 59 key(s); 169 already matched the backup
Saved on the keyboard
```

What it does:

- Matches layers **by position** (first layer of the backup to the first layer of
  the keyboard, and so on) and renames them to the backup's names if needed.
- If the backup has more layers than the keyboard, it adds them from the spare
  layer slots. Layers the keyboard has beyond the backup are left untouched.
- Writes **only the keys that differ**, then saves, exactly like **Save** in ZMK
  Studio. The keymap survives restarts from then on.
- Finds behaviors on the keyboard **by name**, because their numeric ids can be
  different on another keyboard or after a settings reset. Layer parameters (such
  as `&mo RAISE`) are translated to the keyboard's layer ids.
- Checks that the keyboard has the same number of keys as the backup.

A key whose behavior does not exist in the keyboard's firmware (for example a
custom macro that was removed from the keymap) is listed as `Not restored`; the
rest of the keymap is still written and saved, and the script exits with an error
so the problem is not missed.

### From the `.keymap`, by rebuilding the firmware

This makes your layout the firmware's default: any freshly flashed keyboard, or one
whose settings are reset, starts with it.

1. Compare the export with the current file:

   ```sh
   diff config/rev57lp.keymap keymap-backups/rev57lp-<date>-<time>.keymap
   ```

2. If it looks right, copy it over, commit and push:

   ```sh
   cp keymap-backups/rev57lp-<date>-<time>.keymap config/rev57lp.keymap
   git add config/rev57lp.keymap
   git commit -m "Update keymap from ZMK Studio"
   git push
   ```

3. GitHub Actions builds the new firmware. Flash it as usual.

Flashing new firmware does **not** erase the changes stored by Studio: the keyboard
keeps using them. To make it use the keymap compiled into the firmware, choose
**Restore Stock Settings** in ZMK Studio.

## Options

```text
studio_keymap.py export  [--port PORT] [--keymap FILE] [--output-dir DIR]
studio_keymap.py restore BACKUP.json [--port PORT] [--keymap FILE] [--dry-run]
```

| Option | Default | Use it when |
|---|---|---|
| `--port` | Detected: the only `/dev/cu.usbmodem*` (macOS) or `/dev/ttyACM*` (Linux) | Several serial devices are connected |
| `--keymap` | The `.keymap` file in `config/` | Export: another file as template. Restore: the file that defines your custom behaviors |
| `--output-dir` | `keymap-backups/` | Export: you want the files somewhere else |
| `--dry-run` | Off | Restore: only list what would change |

To find the port by hand on macOS, run `ls /dev/cu.usbmodem*` with the keyboard
unplugged and again with it plugged in.

## Troubleshooting

| Message | Cause and fix |
|---|---|
| `No USB serial port found` | The keyboard is not connected over USB, the cable is charge-only, or the output is on Bluetooth. |
| `Several serial ports found` | Pass the right one with `--port`. |
| `Resource busy` or `The keyboard did not answer` | ZMK Studio (browser tab or app) or another program still has the port open: close it and try again. If it persists, the firmware may have been built without ZMK Studio. |
| `Timed out waiting for the keyboard to be unlocked` | The unlock key was not pressed within two minutes. Run the script again. |
| `Some keys could not be identified (behavior not available in this firmware)` | Export: those keys point to a behavior the firmware does not include. They are written as `&none` with a `/* TODO */` note holding the raw values. |
| `Some keys could not be identified (unknown behavior id N)` | Export: the keyboard has a behavior the script does not recognise, for example a custom one that is not defined in the keymap used as template. Same `/* TODO */` handling. |
| `Layers were reordered in Studio` | Export: conditional layers (the LOWER + RAISE = Adjust rule) refer to layers by number. Check them in the exported `.keymap`. |
| `Not restored: ... uses a behavior this firmware does not have` | Restore: the keyboard's firmware lacks that behavior. Add it to the keymap and rebuild, or change that key in Studio. |
| `The backup has N keys per layer but the keyboard has M` | Restore: the backup is from a different keyboard. |
| `The keyboard could not save the changes` | Restore: the changes were written but not saved; they will be lost on restart. Try again. |

Unknown keys are never guessed: a `/* TODO */` note keeps the exact data so it can
be fixed by hand.

## How it works

1. **Serial connection.** The script opens the keyboard's USB serial port in raw
   mode. Studio messages are framed as `0xAB <payload> 0xAD`; any of those bytes
   (or the escape byte `0xAC`) inside the payload is preceded by `0xAC`.
2. **Protocol.** Payloads are protobuf messages from
   [zmk-studio-messages](https://github.com/zmkfirmware/zmk-studio-messages). The
   script encodes and decodes the few message types it needs by hand, which is why
   it needs no dependencies. It uses these requests:
   - `core.get_device_info` and `core.get_lock_state` (waits while locked);
   - `behaviors.list_all_behaviors` and `behaviors.get_behavior_details`;
   - `keymap.get_keymap` and `keymap.get_physical_layouts`;
   - for restore: `keymap.set_layer_binding`, `keymap.add_layer`,
     `keymap.set_layer_props` and `keymap.save_changes`.
3. **Behaviors.** The keyboard refers to behaviors by numeric ids that it assigns
   and keeps in its own settings, so the same behavior can have a different id on
   another keyboard. The script therefore asks the keyboard for each behavior's
   name (its `display-name`, or its devicetree name when it has none) and maps
   that name to a `&label`:
   - built-in ZMK behaviors (`Key Press` → `&kp`, `Mod-Tap` → `&mt`, ...);
   - custom behaviors found in the keymap file (macros, hold-taps), matched by
     node name, `label` or `display-name`.
4. **Parameters.** Each parameter is printed according to what it means for that
   behavior: a keycode (`LCTRL`, `LS(N1)`), a layer (`RAISE`), a Bluetooth command
   (`BT_SEL 2`), an underglow command, an output, and so on.
5. **Keycode names.** The table at the end of the script maps every keycode value
   to all its ZMK names. It was generated with the C preprocessor from ZMK v0.3
   `include/dt-bindings/zmk/keys.h`, so the values are exact.

## Limitations

- Only layers and bindings are handled: that is all ZMK Studio can edit. Macros,
  combos and behavior settings (such as tapping terms) always come from the keymap
  file.
- Written for ZMK v0.3. A later ZMK version may add behaviors or keycodes; they
  show up as `/* TODO */` until the tables in the script are updated.
- Windows is not supported (the script uses POSIX serial port calls).

## Testing

The script was tested against ZMK v0.3 compiled as a `native_posix_64` simulation,
with the Rev57LP physical layout, all behaviors enabled as in the real firmware,
flash storage (NVS) and Studio on a virtual serial port:

- **Export:** lock and unlock, a key changed through the Studio protocol, export,
  a new firmware built from the exported `.keymap`, and a second export that
  matched the first.
- **`.keymap` recovery:** a real export from the keyboard was built for
  `nice_nano_v2`, and all 228 keys in the compiled firmware matched the raw values
  read from the keyboard.
- **Restore:** the same real backup restored onto the stock keymap. The dry run
  wrote nothing; the restore changed the differing keys and saved them; after a
  restart every key matched the backup; a second restore found nothing to change.
  Bluetooth and underglow keys could not be exercised because the simulation has
  neither.
