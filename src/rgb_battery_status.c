/*
 * Battery level on the RGB underglow.
 *
 * Every CONFIG_RGB_BATTERY_STATUS_INTERVAL_SEC seconds, read the battery
 * charge, find its level in the table below and blink the whole strip with
 * that level's colour pattern. Then put the underglow back exactly as it
 * was (on/off, effect and colour).
 *
 * Only ZMK's public underglow API is used. It has no getters for the colour
 * and effect, but calc_hue(0) and calc_effect(0) return the current values
 * unchanged, which is enough to save and restore them.
 *
 * Everything runs as non-blocking delayed work on ZMK's low-priority work
 * queue, the same queue the underglow itself uses, so nothing sleeps.
 */

#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/logging/log.h>

#include <zmk/battery.h>
#include <zmk/rgb_underglow.h>
#include <zmk/workqueue.h>

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

/* ---------------------------------------------------------------------- */
/* Levels: edit this table to change thresholds, colours or blink count.  */
/* ---------------------------------------------------------------------- */

/* Hues on the colour wheel (0-359). */
#define RED 0
#define ORANGE 25
#define GREEN 120

#define MAX_BLINKS 4

struct battery_level {
    uint8_t min_charge;          /* Level applies from this % upwards. */
    uint8_t blinks;              /* How many entries of hues[] to use. */
    uint16_t hues[MAX_BLINKS];   /* Colour of each blink, in order.    */
};

/* Checked top to bottom; the first level whose min_charge is reached wins. */
static const struct battery_level levels[] = {
    {.min_charge = 100, .blinks = 3, .hues = {GREEN, GREEN, GREEN}}, /* full      */
    {.min_charge = 71, .blinks = 2, .hues = {GREEN, GREEN}},         /* 71-99%    */
    {.min_charge = 55, .blinks = 2, .hues = {GREEN, ORANGE}},        /* 55-70%    */
    {.min_charge = 35, .blinks = 2, .hues = {ORANGE, ORANGE}},       /* 35-54%    */
    {.min_charge = 20, .blinks = 2, .hues = {RED, RED}},             /* 20-34%    */
    {.min_charge = 0, .blinks = 3, .hues = {RED, RED, RED}},         /* below 20% */
};

/* ---------------------------------------------------------------------- */

/* Matches UNDERGLOW_EFFECT_SOLID, which is private to rgb_underglow.c. */
#define EFFECT_SOLID 0

/* Wait for the first battery reading before the first check. */
#define FIRST_CHECK_DELAY K_SECONDS(60)

#define CHECK_INTERVAL K_SECONDS(CONFIG_RGB_BATTERY_STATUS_INTERVAL_SEC)
#define BLINK_PHASE K_MSEC(CONFIG_RGB_BATTERY_STATUS_BLINK_MS)

static struct {
    bool active;
    const struct battery_level *level;
    int phase; /* Each blink is one "on" phase followed by one "off" phase. */
    bool was_on;
    int effect;
    struct zmk_led_hsb color;
} blink;

static void check_battery(struct k_work *work);
static void blink_step(struct k_work *work);

static K_WORK_DELAYABLE_DEFINE(check_work, check_battery);
static K_WORK_DELAYABLE_DEFINE(blink_work, blink_step);

__maybe_unused static const struct battery_level *find_level(uint8_t charge) {
    for (size_t i = 0; i < ARRAY_SIZE(levels); i++) {
        if (charge >= levels[i].min_charge) {
            return &levels[i];
        }
    }
    return &levels[ARRAY_SIZE(levels) - 1];
}

static void set_hue(uint16_t hue) {
    struct zmk_led_hsb color = {
        .h = hue,
        .s = 100,
        .b = MIN(CONFIG_RGB_BATTERY_STATUS_BRIGHTNESS, CONFIG_ZMK_RGB_UNDERGLOW_BRT_MAX),
    };
    zmk_rgb_underglow_set_hsb(color);
}

static void restore_underglow(void) {
    zmk_rgb_underglow_select_effect(blink.effect);
    zmk_rgb_underglow_set_hsb(blink.color);

    if (blink.was_on) {
        zmk_rgb_underglow_on();
    } else {
        zmk_rgb_underglow_off();
    }

    blink.active = false;
}

static void blink_step(struct k_work *work) {
    int blink_index = blink.phase / 2;

    if (blink_index >= blink.level->blinks) {
        restore_underglow();
        return;
    }

    if (blink.phase % 2 == 0) {
        set_hue(blink.level->hues[blink_index]);
        zmk_rgb_underglow_on();
    } else {
        zmk_rgb_underglow_off();
    }

    blink.phase++;
    k_work_reschedule_for_queue(zmk_workqueue_lowprio_work_q(), &blink_work, BLINK_PHASE);
}

static void start_blinking(const struct battery_level *level) {
    zmk_rgb_underglow_get_state(&blink.was_on);
    blink.effect = zmk_rgb_underglow_calc_effect(0);
    blink.color = zmk_rgb_underglow_calc_hue(0);
    blink.level = level;
    blink.phase = 0;
    blink.active = true;

    /* Start from a dark strip so the first blink is visible. */
    zmk_rgb_underglow_off();
    zmk_rgb_underglow_select_effect(EFFECT_SOLID);

    k_work_reschedule_for_queue(zmk_workqueue_lowprio_work_q(), &blink_work, BLINK_PHASE);
}

static void check_battery(struct k_work *work) {
    k_work_reschedule_for_queue(zmk_workqueue_lowprio_work_q(), &check_work, CHECK_INTERVAL);

    if (blink.active) {
        return;
    }

#if IS_ENABLED(CONFIG_RGB_BATTERY_STATUS_DEMO)
    static size_t demo_index;
    start_blinking(&levels[demo_index]);
    demo_index = (demo_index + 1) % ARRAY_SIZE(levels);
#else
    uint8_t charge = zmk_battery_state_of_charge();

    /* 0 means "no reading yet"; a keyboard really at 0% is already off. */
    if (charge == 0) {
        return;
    }

    LOG_INF("Battery at %d%%, blinking underglow", charge);
    start_blinking(find_level(charge));
#endif
}

static int rgb_battery_status_init(void) {
    BUILD_ASSERT(ARRAY_SIZE(levels) > 0, "At least one battery level is needed");
    k_work_reschedule_for_queue(zmk_workqueue_lowprio_work_q(), &check_work, FIRST_CHECK_DELAY);
    return 0;
}

SYS_INIT(rgb_battery_status_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
