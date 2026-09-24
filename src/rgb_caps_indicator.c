/*
 * Caps indicator on the RGB underglow.
 *
 * While Caps Lock or Caps Word is on, the whole strip shows a solid colour
 * (cyan by default). When both are off again, the underglow goes back to
 * exactly what it was showing before (on/off, effect and colour).
 *
 * - Caps Lock is the computer's: the keyboard learns it from the host's LED
 *   report, the same signal that lights the Caps Lock LED on a regular keyboard.
 *   ZMK raises a zmk_hid_indicators_changed event whenever it changes.
 *
 * - Caps Word (&caps_word) is a ZMK behavior that raises no event. It can only
 *   turn on or off while a key is being handled (its own key, or a key that ends
 *   the word), so after every key event this feature reads its state. See
 *   caps_word_is_active() for how.
 *
 * ZMK saves the underglow state to flash about a minute after every change. If
 * the keyboard were switched off while the caps colour was shown, it would boot
 * with it. To avoid that, the state from before the caps colour is also kept in
 * our own settings entry and put back at the next boot.
 *
 * Only ZMK's public underglow API is used, from the low-priority work queue.
 */

#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>

#include <dt-bindings/zmk/hid_usage.h>
#include <zmk/event_manager.h>
#include <zmk/events/keycode_state_changed.h>
#include <zmk/events/position_state_changed.h>
#include <zmk/rgb_underglow.h>
#include <zmk/workqueue.h>

#if IS_ENABLED(CONFIG_RGB_CAPS_INDICATOR_CAPS_LOCK)
#include <zmk/events/hid_indicators_changed.h>
#endif

#if IS_ENABLED(CONFIG_RGB_BATTERY_STATUS)
#include "rgb_battery_status.h"
#endif

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

/* HID LED report bits follow the LED usage ids: bit 0 is Num Lock, bit 1 Caps Lock. */
#define CAPS_LOCK_BIT BIT(HID_USAGE_LED_CAPS_LOCK - 1)

/* Matches UNDERGLOW_EFFECT_SOLID, which is private to rgb_underglow.c. */
#define EFFECT_SOLID 0

/* How often to check again while a battery blink owns the underglow. */
#define WAIT_FOR_BATTERY_BLINK K_MSEC(100)

#define SETTINGS_SUBTREE "rgb_caps"
#define SETTINGS_KEY SETTINGS_SUBTREE "/previous"

/* Caps Word is only built into the firmware when the keymap (or ZMK Studio) can use it. */
#define CAPS_WORD_NODE DT_NODELABEL(caps_word)
#define HAS_CAPS_WORD                                                                              \
    (IS_ENABLED(CONFIG_RGB_CAPS_INDICATOR_CAPS_WORD) && DT_NODE_HAS_STATUS(CAPS_WORD_NODE, okay))

/* Underglow state from before the caps colour; stored in settings as-is. */
struct previous_underglow {
    bool caps_color_shown;
    bool was_on;
    uint8_t effect;
    struct zmk_led_hsb color;
};

static struct previous_underglow previous;
static bool caps_lock_on;
static bool caps_word_on;

static void apply_caps_state(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(apply_work, apply_caps_state);

static void schedule_apply(k_timeout_t delay) {
    k_work_reschedule_for_queue(zmk_workqueue_lowprio_work_q(), &apply_work, delay);
}

#if HAS_CAPS_WORD
/*
 * ZMK v0.3 keeps the Caps Word state private: behavior_caps_word.c stores it as
 * the only field of the device data, `struct behavior_caps_word_data { bool active; }`.
 * This reads that field. If a later ZMK version changes that struct, update this
 * function (the ZMK version is pinned in config/west.yml).
 */
static bool caps_word_is_active(void) {
    const struct device *caps_word = DEVICE_DT_GET(CAPS_WORD_NODE);
    return *(const bool *)caps_word->data;
}
#endif

static void store_previous_state(void) {
#if IS_ENABLED(CONFIG_SETTINGS)
    int err = settings_save_one(SETTINGS_KEY, &previous, sizeof(previous));
    if (err < 0) {
        LOG_WRN("Could not store the underglow state for the caps indicator: %d", err);
    }
#endif
}

static void show_caps_color(void) {
    struct zmk_led_hsb caps_color = {
        .h = CONFIG_RGB_CAPS_INDICATOR_HUE,
        .s = 100,
        .b = MIN(CONFIG_RGB_CAPS_INDICATOR_BRIGHTNESS, CONFIG_ZMK_RGB_UNDERGLOW_BRT_MAX),
    };

    zmk_rgb_underglow_get_state(&previous.was_on);
    previous.effect = zmk_rgb_underglow_calc_effect(0);
    previous.color = zmk_rgb_underglow_calc_hue(0);
    previous.caps_color_shown = true;
    store_previous_state();

    zmk_rgb_underglow_select_effect(EFFECT_SOLID);
    zmk_rgb_underglow_set_hsb(caps_color);
    zmk_rgb_underglow_on();
}

static void restore_previous_state(void) {
    zmk_rgb_underglow_select_effect(previous.effect);
    zmk_rgb_underglow_set_hsb(previous.color);

    if (previous.was_on) {
        zmk_rgb_underglow_on();
    } else {
        zmk_rgb_underglow_off();
    }

    previous.caps_color_shown = false;
    store_previous_state();
}

static void apply_caps_state(struct k_work *work) {
#if IS_ENABLED(CONFIG_RGB_BATTERY_STATUS)
    /* Let a battery blink finish first: it restores the state it saved. */
    if (rgb_battery_status_is_blinking()) {
        schedule_apply(WAIT_FOR_BATTERY_BLINK);
        return;
    }
#endif

#if HAS_CAPS_WORD
    caps_word_on = caps_word_is_active();
#endif

    bool show = caps_lock_on || caps_word_on;
    if (show == previous.caps_color_shown) {
        return;
    }

    LOG_INF("Caps indicator %s (Caps Lock %d, Caps Word %d)", show ? "on" : "off", caps_lock_on,
            caps_word_on);
    if (show) {
        show_caps_color();
    } else {
        restore_previous_state();
    }
}

#if IS_ENABLED(CONFIG_RGB_CAPS_INDICATOR_CAPS_LOCK)
static int on_hid_indicators_changed(const zmk_event_t *event) {
    const struct zmk_hid_indicators_changed *changed = as_zmk_hid_indicators_changed(event);
    if (changed) {
        caps_lock_on = (changed->indicators & CAPS_LOCK_BIT) != 0;
        schedule_apply(K_NO_WAIT);
    }
    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(rgb_caps_indicator_caps_lock, on_hid_indicators_changed);
ZMK_SUBSCRIPTION(rgb_caps_indicator_caps_lock, zmk_hid_indicators_changed);
#endif

#if HAS_CAPS_WORD
/*
 * Runs for every key event. The check itself is scheduled as work so that it
 * happens after ZMK has finished handling the event, Caps Word included.
 */
static int on_key_event(const zmk_event_t *event) {
    schedule_apply(K_NO_WAIT);
    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(rgb_caps_indicator_caps_word, on_key_event);
ZMK_SUBSCRIPTION(rgb_caps_indicator_caps_word, zmk_position_state_changed);
ZMK_SUBSCRIPTION(rgb_caps_indicator_caps_word, zmk_keycode_state_changed);
#endif

#if IS_ENABLED(CONFIG_SETTINGS)

static int load_previous_state(const char *name, size_t len, settings_read_cb read_cb,
                               void *cb_arg) {
    if (!settings_name_steq(name, "previous", NULL)) {
        return -ENOENT;
    }
    if (len != sizeof(previous)) {
        return -EINVAL; /* Stored by an older, incompatible version: ignore it. */
    }
    int read = read_cb(cb_arg, &previous, sizeof(previous));
    return read < 0 ? read : 0;
}

static int after_settings_loaded(void) {
    /*
     * Caps Lock and Caps Word always start off. If the keyboard was switched off
     * while the caps colour was shown, this puts the previous state back; if the
     * host reports Caps Lock on once connected, the colour comes back then.
     */
    if (previous.caps_color_shown) {
        schedule_apply(K_NO_WAIT);
    }
    return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(rgb_caps_indicator, SETTINGS_SUBTREE, NULL, load_previous_state,
                               after_settings_loaded, NULL);

#endif /* IS_ENABLED(CONFIG_SETTINGS) */
