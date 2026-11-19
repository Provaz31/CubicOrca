#pragma once

#include "AnycubicCloudService.hpp"
#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>

namespace Slic3r {

// A background request. The worker owns its state and never captures a window.
// Read-only callers may cancel on view changes. Explicit commands must finish
// without automatic cancellation or replay; their UI may discard the result.
class AnycubicCloudReadTask {
public:
    using Read = std::function<AnycubicCloudResult(AnycubicCloudService::CancelFn)>;
    using StreamRead = std::function<AnycubicCloudResult(AnycubicCloudService::CancelFn, AnycubicCloudService::UpdateFn)>;

    static std::shared_ptr<AnycubicCloudReadTask> start(Read read)
    {
        return start_stream([read = std::move(read)](AnycubicCloudService::CancelFn cancelled, AnycubicCloudService::UpdateFn) {
            return read(std::move(cancelled));
        });
    }

    static std::shared_ptr<AnycubicCloudReadTask> start_stream(StreamRead read)
    {
        auto task = std::make_shared<AnycubicCloudReadTask>();
        try {
            std::thread([task, read = std::move(read)]() {
                try {
                    task->m_result = read([task]() { return task->cancelled(); }, [task](const nlohmann::json& data) {
                        std::lock_guard<std::mutex> lock(task->m_update_mutex);
                        task->m_update = data;
                        task->m_has_update = true;
                    });
                } catch (...) {
                    task->m_result.success = false;
                    task->m_result.error = "worker_failed";
                }
                task->m_ready.store(true, std::memory_order_release);
            }).detach();
        } catch (...) {
            task->m_result.error = "worker_failed";
            task->m_ready.store(true, std::memory_order_release);
        }
        return task;
    }

    void cancel() { m_cancelled.store(true); }
    bool cancelled() const { return m_cancelled.load(); }
    bool ready() const { return m_ready.load(std::memory_order_acquire); }
    bool take_update(nlohmann::json& data)
    {
        std::lock_guard<std::mutex> lock(m_update_mutex);
        if (!m_has_update) return false;
        data = std::move(m_update);
        m_has_update = false;
        return true;
    }
    // Read only after ready() returned true; publication uses release/acquire.
    const AnycubicCloudResult& result() const { return m_result; }

private:
    std::atomic<bool> m_cancelled{false};
    std::atomic<bool> m_ready{false};
    AnycubicCloudResult m_result;
    std::mutex m_update_mutex;
    nlohmann::json m_update;
    bool m_has_update{false};
};

} // namespace Slic3r
