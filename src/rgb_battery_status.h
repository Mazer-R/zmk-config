/*
 * Battery status on the RGB underglow: what other features need to know.
 */

#pragma once

#include <stdbool.h>

/*
 * True while a battery blink sequence owns the underglow. Other features that
 * change the underglow should wait until it is false, so the blink sequence
 * restores the state they expect.
 */
bool rgb_battery_status_is_blinking(void);
