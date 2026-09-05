// Read the input-power status of the SY6974B charger on the reTerminal E1002.
// The SY6974B register map follows the BQ25601 family: REG08 is the system
// status register.
//   bit 7..5  VBUS_STAT   000 = no input, 001 = USB host, 010 = adapter, 111 = OTG
//   bit 4..3  CHRG_STAT   00 = not charging, 01 = pre-charge, 10 = fast, 11 = done
//   bit 2     PG_STAT     1 = input power good
// Included from e1002.yaml via `esphome: includes:`.
#pragma once

#include "esphome/components/i2c/i2c.h"

namespace deskmate {

static constexpr uint8_t CHARGER_ADDRESS = 0x6B;
static constexpr uint8_t CHARGER_REG_STATUS = 0x08;

class ChargerProbe : public esphome::i2c::I2CDevice {
 public:
  ChargerProbe(esphome::i2c::I2CBus *bus) {
    this->set_i2c_bus(bus);
    this->set_i2c_address(CHARGER_ADDRESS);
  }
  // Returns REG08, or -1 when the charger does not answer.
  int read_status() {
    uint8_t value = 0;
    if (this->read_register(CHARGER_REG_STATUS, &value, 1) != esphome::i2c::ERROR_OK)
      return -1;
    return value;
  }
};

// True when the charger reports input power (USB) present. `status` is REG08.
inline bool usb_present(int status) {
  if (status < 0)
    return false;
  const int vbus_stat = (status >> 5) & 0x07;
  const bool power_good = (status & 0x04) != 0;
  return power_good || (vbus_stat != 0 && vbus_stat != 7);
}

inline const char *charge_state_name(int status) {
  if (status < 0)
    return "unknown";
  switch ((status >> 3) & 0x03) {
    case 0:
      return "not_charging";
    case 1:
      return "pre_charge";
    case 2:
      return "charging";
    default:
      return "charged";
  }
}

}  // namespace deskmate
