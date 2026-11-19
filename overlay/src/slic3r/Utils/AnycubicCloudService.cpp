#include "AnycubicCloudService.hpp"

#include "libslic3r/Utils.hpp"
#include "slic3r/plugin/PythonInterpreter.hpp"
#include <boost/filesystem.hpp>
#include <boost/process.hpp>
#ifdef _WIN32
#include <boost/process/windows.hpp>
#endif
#include <chrono>
#include <future>
#include <ostream>
#include <system_error>
#include <utility>

namespace Slic3r {
namespace {
using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

struct WorkerCleanup {
    boost::process::child& process;
    boost::process::opstream& input;
    ~WorkerCleanup()
    {
        std::error_code error;
        if (process.running(error))
            process.terminate(error);
        try { input.pipe().close(); } catch (...) {}
        process.wait(error);
    }
};

AnycubicCloudResult failure(const char* code, bool cleanup_pending = false)
{
    AnycubicCloudResult result;
    result.error = code;
    result.cleanup_pending = cleanup_pending;
    return result;
}
} // namespace

AnycubicCloudResult AnycubicCloudService::request(const std::string& method, const json& arguments,
                                                ProgressFn progress, CancelFn cancelled, UpdateFn update)
{
    std::lock_guard<std::mutex> lock(m_mutex);
    // Starting a print requires a prepared one-shot ticket and GUI confirmation.
    if (method != "account" && method != "exchange_session" && method != "import_session" && method != "logout" &&
        method != "list_printers" && method != "workbench_snapshot" && method != "monitor" && method != "camera" && method != "upload_gcode" && method != "prepare_print" &&
        method != "start_print" && method != "clear_print_lock" && method != "set_temperature" && method != "peripheral_control" && method != "review_job_control" && method != "execute_job_control" && method != "cloud_library" && method != "motion_control" && method != "job_preview" && method != "review_update" && method != "extract_update")
        return failure("invalid_request");
    if ((method == "start_print" || method == "clear_print_lock" || method == "camera" || method == "set_temperature" || method == "peripheral_control" || method == "execute_job_control" || method == "motion_control" || method == "extract_update") &&
        (!arguments.contains("confirmed") || arguments["confirmed"] != true))
        return failure("invalid_request");
    if (!arguments.is_object())
        return failure("invalid_request");
    if (cancelled && cancelled())
        return failure("cancelled");
    try {
        const auto script = boost::filesystem::path(resources_dir()) / "scripts" / "anycubic_cloud_worker.py";
        const auto session_path = boost::filesystem::path(data_dir()) / "anycubic-cloud" / "secrets" / "session.sec";
        const auto python = PythonInterpreter::bundled_python_executable();
        if (python.empty() || !boost::filesystem::is_regular_file(script))
            return failure("worker_unavailable");

        boost::process::opstream input;
        boost::process::ipstream output;
        // Credentials travel only through the private stdin pipe. Never put them
        // in process arguments, an environment variable or a webview message.
        boost::process::child process(python, "-I", "-B", script.string(),
            boost::process::std_in < input, boost::process::std_out > output,
            boost::process::std_err > boost::process::null,
#ifdef _WIN32
            boost::process::windows::create_no_window,
#endif
            boost::process::start_dir(script.parent_path().string()));

        auto response = std::async(std::launch::async, [&]() -> json {
            std::string line;
            while (std::getline(output, line)) {
                if (line.size() > 2 * 1024 * 1024)
                    return json();
                auto message = json::parse(line, nullptr, false);
                if (message.is_discarded() || !message.is_object())
                    return json();
                if (message.value("type", "") == "result")
                    return message;
                if ((method == "monitor" || method == "camera") && message.value("type", "") == "update" && update &&
                    message.contains("data") && message["data"].is_object()) {
                    try { update(message["data"]); } catch (...) {}
                }
                if (message.value("type", "") == "progress" && progress) {
                    try { progress(message.value("phase", ""), message.value("fraction", 0.0)); }
                    catch (...) {}
                }
            }
            return json();
        });
        // Construct after the future: on any exception, terminate the child
        // before waiting for its stdout reader's future to finish.
        WorkerCleanup cleanup{process, input};
        json request = {{"method", method}, {"args", arguments}, {"session_path", session_path.string()}};
        const auto serialized = request.dump();
        if (serialized.size() > 131071)
            return failure("invalid_request");
        input << serialized << '\n' << std::flush;
        if (!input)
            return failure(method == "start_print" ? "start_outcome_unknown" : "worker_failed");
        const auto started = Clock::now();
        auto cancelled_at = started;
        bool cancel_sent = false;
        while (response.wait_for(std::chrono::milliseconds(100)) != std::future_status::ready) {
            if (!cancel_sent && cancelled && cancelled()) {
                if (method != "upload_gcode" && method != "prepare_print" && method != "camera")
                    return failure(method == "start_print" ? "start_outcome_unknown" : "cancelled");
                input << "{\"method\":\"cancel\"}\n" << std::flush;
                cancel_sent = true;
                cancelled_at = Clock::now();
            }
            if ((method != "camera" && Clock::now() - started > std::chrono::minutes(6)) ||
                (cancel_sent && Clock::now() - cancelled_at > std::chrono::seconds(method == "camera" ? 25 : 90)))
                return failure(method == "start_print" ? "start_outcome_unknown" : "worker_timeout",
                               method == "upload_gcode" || method == "prepare_print" || method == "camera");
        }
        const auto message = response.get();
        if (!message.is_object() || !message.contains("ok") || !message["ok"].is_boolean())
            return failure(method == "start_print" ? "start_outcome_unknown" : "worker_failed",
                           method == "upload_gcode" || method == "prepare_print" || method == "camera");
        AnycubicCloudResult result;
        result.success = message["ok"].get<bool>();
        result.cleanup_pending = message.value("cleanup_pending", false);
        if (result.success) {
            result.data = message.value("data", json::object());
            result.cleanup_pending = result.data.value("cleanup_pending", result.cleanup_pending);
        } else {
            result.error = message.value("error", "operation_failed");
            if (method == "start_print" && result.error == "operation_failed")
                result.error = "start_outcome_unknown";
            if (message.contains("api_code") && message["api_code"].is_number_integer())
                result.data["api_code"] = message["api_code"];
        }
        return result;
    } catch (...) {
        // In particular, do not log the request, child output or raw exception.
        return failure(method == "start_print" ? "start_outcome_unknown" : "worker_failed",
                           method == "upload_gcode" || method == "prepare_print" || method == "camera");
    }
}

} // namespace Slic3r
