#pragma once

#include <functional>
#include <mutex>
#include <string>
#include <nlohmann/json.hpp>

namespace Slic3r {

struct AnycubicCloudResult {
    bool success{false};
    std::string error;
    bool cleanup_pending{false};
    nlohmann::json data = nlohmann::json::object();
};

// Application-owned service, independent of the plugin registry and the global
// Orca Cloud login. Call on a background job; progress callbacks run on a worker.
class AnycubicCloudService {
public:
    using ProgressFn = std::function<void(const std::string&, double)>;
    using CancelFn = std::function<bool()>;
    using UpdateFn = std::function<void(const nlohmann::json&)>;

    AnycubicCloudResult request(const std::string& method,
                               const nlohmann::json& arguments = nlohmann::json::object(),
                               ProgressFn progress = {}, CancelFn cancelled = {}, UpdateFn update = {});

private:
    std::mutex m_mutex;
};

} // namespace Slic3r
