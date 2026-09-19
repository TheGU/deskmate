// Page id lookup for the reTerminal E1002 firmware. The device holds no
// fixed page list: it learns the ids, in order, from the telemetry
// response's comma-joined "pages" field (see page_names in e1002.yaml) and
// looks one up by index here.
// Included from e1002.yaml via `esphome: includes:`.
#pragma once

#include <string>

namespace deskmate {

// Returns the idx-th comma-separated id in names, or "" when idx is out of
// range or names is empty (no telemetry response received yet this
// session, so the device does not know any ids).
inline std::string page_name(const std::string &names, int idx) {
  if (names.empty() || idx < 0)
    return "";
  size_t start = 0;
  int i = 0;
  while (start <= names.size()) {
    size_t comma = names.find(',', start);
    std::string field = (comma == std::string::npos) ? names.substr(start) : names.substr(start, comma - start);
    if (i == idx)
      return field;
    if (comma == std::string::npos)
      break;
    start = comma + 1;
    i++;
  }
  return "";
}

}  // namespace deskmate
