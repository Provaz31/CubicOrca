#pragma once

#include <string>
#include <string_view>

namespace Slic3r::PluginAuditDetail {

// These events have no destination. Persist the user's approval for the event
// itself; socket.connect and DNS events still require their own target grants.
inline std::string targetless_network_permission_key(std::string_view event_name)
{
    if (event_name == "socket.__new__" || event_name == "socket.gethostname")
        return "event:" + std::string(event_name);
    return {};
}

} // namespace Slic3r::PluginAuditDetail
