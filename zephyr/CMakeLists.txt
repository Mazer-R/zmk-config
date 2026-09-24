# Custom firmware features for this keyboard.
# Each feature is one file in src/ and is only built when its Kconfig
# option is enabled (see zephyr/Kconfig and config/rev57lp.conf).

set(SRC_DIR ${CMAKE_CURRENT_LIST_DIR}/../src)

target_sources_ifdef(CONFIG_RGB_BATTERY_STATUS app PRIVATE ${SRC_DIR}/rgb_battery_status.c)
target_sources_ifdef(CONFIG_RGB_CAPS_INDICATOR app PRIVATE ${SRC_DIR}/rgb_caps_indicator.c)
