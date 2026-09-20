// Helpers for battery mode on the reTerminal E1002 (ESP32-S3).
// Included from e1002.yaml via `esphome: includes:`.
#pragma once

#include <cstdint>
#include "driver/gpio.h"
#include "driver/rtc_io.h"
#include "esp_sleep.h"

namespace deskmate {

// The three top buttons are active low. For ext1 ANY_LOW wake the pull-ups
// must stay alive while the digital domain is powered down, so switch the
// pads to RTC control with an internal pull-up and keep the RTC peripheral
// domain powered. Also pin down the pads that must not float during sleep:
// the battery-divider enable (GPIO21, off = low) and the LED (GPIO6,
// inverted, off = high). The buzzer gate (GPIO45, not an RTC pad) is held
// low through the digital hold feature.
inline void prepare_sleep_pads() {
  for (int p : {3, 4, 5}) {
    auto g = static_cast<gpio_num_t>(p);
    rtc_gpio_init(g);
    rtc_gpio_set_direction(g, RTC_GPIO_MODE_INPUT_ONLY);
    rtc_gpio_pulldown_dis(g);
    rtc_gpio_pullup_en(g);
  }
  for (int p : {6, 21}) {
    auto g = static_cast<gpio_num_t>(p);
    rtc_gpio_init(g);
    rtc_gpio_set_direction(g, RTC_GPIO_MODE_OUTPUT_ONLY);
    rtc_gpio_set_level(g, p == 6 ? 1 : 0);
  }
  gpio_set_level(GPIO_NUM_45, 0);
  gpio_hold_en(GPIO_NUM_45);
  gpio_deep_sleep_hold_en();
  esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON);
}

// Undo prepare_sleep_pads() after a wake, before ESPHome configures the pins
// as digital GPIO (IDF requires rtc_gpio_deinit before reusing the pad).
inline void release_sleep_pads() {
  for (int p : {3, 4, 5, 6, 21}) {
    rtc_gpio_deinit(static_cast<gpio_num_t>(p));
  }
  gpio_deep_sleep_hold_dis();
  gpio_hold_dis(GPIO_NUM_45);
}

// Seconds from now_seconds_of_day until the next slot in hour_mask (bit h set
// means wake at local hour h; see wake_hours_mask in e1002.yaml). Slots
// closer than margin seconds are skipped so a wake that ran a little late
// does not immediately re-arm for the same slot. A mask iterated bit 0
// upward visits hours in ascending order on its own, so the caller never has
// to sort anything.
inline uint32_t seconds_until_next_slot(int now_seconds_of_day, uint32_t hour_mask, int margin = 120) {
  int first = -1;
  for (int h = 0; h < 24; h++) {
    if (!(hour_mask & (1u << h)))
      continue;
    int t = h * 3600;
    if (first < 0)
      first = t;
    if (t - now_seconds_of_day >= margin)
      return static_cast<uint32_t>(t - now_seconds_of_day);
  }
  if (first < 0)
    return 3600;
  return static_cast<uint32_t>(first + 86400 - now_seconds_of_day);
}

// ClickTrigger scores a click as millis() - start_time_, and start_time_ is
// only set by a press callback. After an ext1 wake the button is already down
// when the binary_sensor is set up, the initial ON fires no callback, and the
// release that follows is scored as a click of millis() ms, which lands in the
// long-press window. The ext1 on_wake trigger already performed that button's
// action, so the first click on the waking pin must be swallowed.
inline bool consume_wake_click(int &wake_pin, int pin) {
  if (wake_pin != pin)
    return false;
  wake_pin = -1;
  return true;
}

}  // namespace deskmate
