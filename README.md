# Rev57LP ZMK config

[ZMK](https://zmk.dev) firmware configuration for a
[Rev57LP](https://github.com/piit79/rev57lp) keyboard: 57 keys (4 rows of 6 + 6,
one centre key between the halves and 8 thumb keys) with WS2812 RGB underglow,
driven by a **nice!nano v2**. It uses ZMK v0.3 with
[ZMK Studio](https://zmk.studio) enabled.

## Repository layout

```text
.
├── build.yaml                      Boards and shields GitHub Actions builds
├── config/
│   ├── west.yml                    ZMK version (v0.3)
│   ├── rev57lp.keymap              Default keymap compiled into the firmware
│   └── rev57lp.conf                Firmware options (underglow, battery status...)
├── boards/shields/rev57lp/         The keyboard itself: matrix, physical layout, underglow pin
├── zephyr/                         Makes this repo a Zephyr module
│   ├── module.yml
│   ├── Kconfig                     Options of the custom firmware features
│   └── CMakeLists.txt              Builds each feature only when it is enabled
├── src/                            Custom firmware features, one file each
│   └── rgb_battery_status.c        Battery level on the underglow
├── scripts/studio-keymap/          Tool to back up the keymap stored by ZMK Studio
└── keymap-backups/                 Backups written by that tool
```

## Building and flashing

Every push runs GitHub Actions, which builds the firmware from `build.yaml`
(`nice_nano_v2` + `rev57lp`, with ZMK Studio over USB).

1. Open the repository's **Actions** tab, pick the latest run of your branch and
   download the **firmware** artifact at the bottom of its summary page.
2. Put the keyboard in bootloader mode, either:
   - press reset twice quickly (or short **RST** and **GND** twice), or
   - press **Adjust + top-left key** (the `&bootloader` binding).
3. A USB drive called `NICENANO` appears. Copy the `.uf2` file onto it. The drive
   disappears when flashing is done.

Flashing does not erase keymap changes made with ZMK Studio; to save and
recover them, see [Keymap backups](#keymap-backups).

## Layers

As compiled into the firmware from `config/rev57lp.keymap`. Changes made with ZMK
Studio are stored on the keyboard and can differ.

| Layer | How to reach it | Contents |
|---|---|---|
| Default | Always on | Letters, numbers, modifiers; `Esc` is Ctrl when held, `Enter` is Shift when held |
| Lower | Hold **LOWER** (left thumb, next to Alt) | Shifted symbols, F1-F12, media keys |
| Raise | Hold **RAISE** (right thumb, next to the right Space) | Numbers, F1-F12, brackets, Page Up/Down |
| Adjust | Hold **LOWER + RAISE** together | Keyboard settings, see below |

Adjust is a conditional layer: it turns on whenever Lower and Raise are both
active, so any key bound to "Momentary Layer: Lower/Raise" works to reach it.

| Adjust key | Action |
|---|---|
| Top-left | Bootloader (flashing mode) |
| Top-right | Unlock ZMK Studio |
| `Tab` | Forget the pairing of the current Bluetooth profile |
| `Q` to `T` | Select Bluetooth profile 0 to 4 |
| `Esc` | Toggle output between USB and Bluetooth |
| `←` / `→` | Previous / next underglow effect |

The symbols follow the US layout: with macOS set to Spanish, keys print what the
Spanish layout assigns to them (for example `;` prints `ñ`).

## ZMK Studio

ZMK Studio edits the keymap live, with no rebuilding or flashing.

1. Connect the keyboard over USB (a data cable) and make sure its output is USB
   (Adjust + `Esc` toggles it).
2. Open <https://zmk.studio> in Chrome or Edge (or use the desktop app) and choose
   **USB**, then the keyboard's serial port.
3. Unlock it with **LOWER + RAISE + top-right key**.
4. Edit keys and press **Save**. Changes are stored on the keyboard.

The keymap has two spare layers (`status = "reserved"`) that Studio can turn into
new layers. Once Studio has been used, the keyboard keeps its own copy of the
keymap: editing `config/rev57lp.keymap` has no effect on it until you choose
**Restore Stock Settings** in Studio.

## Battery status on the underglow

Every 10 minutes the underglow blinks a colour pattern for the battery level, then
returns to whatever it was showing (colour, effect, on or off).

| Battery | Blinks |
|---|---|
| 100% | green, green, green |
| 71-99% | green, green |
| 55-70% | green, orange |
| 35-54% | orange, orange |
| 20-34% | red, red |
| below 20% | red, red, red |

- **Levels and colours** are the table at the top of `src/rgb_battery_status.c`,
  one level per line.
- **Interval, blink length and brightness** are options in
  `config/rev57lp.conf`; all of them are described in `zephyr/Kconfig`.
- **Demo mode** (`CONFIG_RGB_BATTERY_STATUS_DEMO=y`, ideally with a short
  interval such as `CONFIG_RGB_BATTERY_STATUS_INTERVAL_SEC=15`) shows each level in
  turn instead of the real one, to check the colours.
- The first check happens one minute after the keyboard starts.
- While charging, the nice!nano measures a higher voltage, so the level shown can
  be higher than the real charge.

## Adding a custom firmware feature

Custom features live in this repository as a Zephyr module, so they do not need a
fork of ZMK:

1. Put the code in a new file in `src/`.
2. Add a `menuconfig` block with its options to `zephyr/Kconfig`.
3. Add one `target_sources_ifdef(CONFIG_<OPTION> app PRIVATE ${SRC_DIR}/<file>.c)`
   line to `zephyr/CMakeLists.txt`.
4. Enable it in `config/rev57lp.conf`.

`src/rgb_battery_status.c` is a complete example.

## Keymap backups

`scripts/studio-keymap/studio_keymap.py` reads the keymap stored on the keyboard,
including every ZMK Studio change, and saves it as JSON plus a ready-to-build
`.keymap` in `keymap-backups/`. It can also write a JSON backup back to the
keyboard:

```sh
python3 scripts/studio-keymap/studio_keymap.py export
python3 scripts/studio-keymap/studio_keymap.py restore keymap-backups/<file>.json
```

See [scripts/studio-keymap/README.md](scripts/studio-keymap/README.md) for
requirements, both ways of recovering a layout (restoring the JSON or building
the `.keymap` into the firmware), and troubleshooting.
