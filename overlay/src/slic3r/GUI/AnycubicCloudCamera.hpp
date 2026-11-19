#pragma once

#include <functional>
#include <memory>
#include <nlohmann/json.hpp>
#include <wx/panel.h>

namespace Slic3r::GUI {

class AnycubicCloudCamera final : public wxPanel {
public:
    using Printer = std::function<nlohmann::json()>;
    AnycubicCloudCamera(wxWindow* parent, Printer printer, std::function<bool()> signed_in);
    ~AnycubicCloudCamera() override;
    void stop();

private:
    struct State;
    std::unique_ptr<State> m_state;
    void start();
    void begin_request();
    void poll();
    void fullscreen(bool enter);
    void message(const wxString& text);
    void stop_viewer();
};

} // namespace Slic3r::GUI
