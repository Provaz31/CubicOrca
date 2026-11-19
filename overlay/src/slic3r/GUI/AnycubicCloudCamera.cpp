#include "AnycubicCloudCamera.hpp"
#include "I18N.hpp"
#include "libslic3r/Utils.hpp"
#include "slic3r/Utils/AnycubicCloudReadTask.hpp"
#include "slic3r/Utils/AnycubicCloudService.hpp"
#include <chrono>
#include <set>
#include <string>
#include <utility>
#include <wx/button.h>
#include <wx/event.h>
#include <wx/filesys.h>
#include <wx/filename.h>
#include <wx/frame.h>
#include <wx/gdicmn.h>
#include <wx/sizer.h>
#include <wx/statbox.h>
#include <wx/stattext.h>
#include <wx/timer.h>
#include <wx/toplevel.h>
#include <wx/webview.h>
#include <wx/window.h>

namespace Slic3r::GUI {
using json = nlohmann::json;
// UI-thread ownership also covers a view rebuilt during a language/profile
// change. The previous worker must finish its close order before another opens.
static std::weak_ptr<AnycubicCloudReadTask> camera_owner;

struct AnycubicCloudCamera::State {
    Printer printer;
    std::function<bool()> signed_in;
    std::shared_ptr<AnycubicCloudReadTask> task;
    wxPanel* display{nullptr};
    wxWebView* viewer{nullptr};
    wxButton *start{nullptr}, *stop{nullptr}, *full{nullptr};
    wxStaticText* status{nullptr};
    wxSizer* outer{nullptr};
    wxFrame* frame{nullptr};
    wxTimer timer;
    wxString url, error_after_stop;
    json printer_id;
    unsigned generation{0};
    bool ready{false}, requested{false}, pending{false}, active{false}, closed{false};
    std::chrono::steady_clock::time_point loading_deadline;
};

AnycubicCloudCamera::AnycubicCloudCamera(wxWindow* parent, Printer printer, std::function<bool()> signed_in)
    : wxPanel(parent), m_state(std::make_unique<State>())
{
    auto& s = *m_state;
    s.printer = std::move(printer);
    s.signed_in = std::move(signed_in);
    auto* box = new wxStaticBoxSizer(wxVERTICAL, this, _L("Camera"));
    s.outer = box;
    s.display = new wxPanel(this);
    auto* layout = new wxBoxSizer(wxVERTICAL);
#ifdef __WXMSW__
    s.viewer = wxWebView::New(s.display, wxID_ANY, "about:blank", wxDefaultPosition,
                            FromDIP(wxSize(480, 270)), wxWebViewBackendEdge);
#else
    s.viewer = wxWebView::New(s.display, wxID_ANY);
#endif
    if (s.viewer) {
        s.viewer->SetMinSize(FromDIP(wxSize(320, 200)));
        s.viewer->EnableAccessToDevTools(false);
        s.viewer->EnableContextMenu(false);
        s.viewer->SetPage("<html><body style='background:#080b10'></body></html>", "about:blank");
        layout->Add(s.viewer, 1, wxEXPAND);
        const auto file = wxString::FromUTF8(resources_dir()) + "/web/anycubic_camera/index.html";
        s.url = wxFileSystem::FileNameToURL(wxFileName(file));
        if (!s.viewer->AddScriptMessageHandler("camera")) s.viewer->Disable();
        s.viewer->Bind(wxEVT_WEBVIEW_NAVIGATING, [this](wxWebViewEvent& e) {
            if (e.GetURL() != m_state->url && e.GetURL() != "about:blank") e.Veto();
        });
        s.viewer->Bind(wxEVT_WEBVIEW_NEWWINDOW, [](wxWebViewEvent& e) { e.Veto(); });
        s.viewer->Bind(wxEVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, [this](wxWebViewEvent& e) {
            auto& s = *m_state;
            const auto raw = std::string(e.GetString().ToUTF8());
            if (raw.size() > 1024) return;
            const auto data = json::parse(raw, nullptr, false);
            if (!data.is_object() || !data.contains("event") || !data["event"].is_string() ||
                !data.contains("generation") || !data["generation"].is_number_unsigned()) return;
            const auto event = data["event"].get<std::string>();
            if (event == "ready" && s.requested) {
                s.ready = true;
                if (s.pending) begin_request();
                return;
            }
            if (data["generation"] != s.generation) return;
            if (event == "exit_fullscreen") fullscreen(false);
            else if (event == "playing" && s.active) message(_L("Camera: receiving video frames"));
            else if (event == "waiting_encryption" && s.active)
                message(_L("Camera: waiting for encrypted video (up to 30 seconds)..."));
            else if (event == "error" && s.active) {
                stop();
                // Accept only fixed diagnostic identifiers. SDK messages and
                // arbitrary exception strings can contain camera credentials.
                static const std::set<std::string> stages = {
                    "create", "role", "encryption", "join", "subscribe", "connection", "token", "frames", "csp"};
                static const std::set<std::string> reasons = {
                    "INVALID_TOKEN", "TOKEN_EXPIRED", "CAN_NOT_GET_GATEWAY_SERVER", "NETWORK_ERROR",
                    "NETWORK_TIMEOUT", "NOT_SUPPORTED", "INVALID_PARAMS", "UID_CONFLICT", "OPERATION_ABORTED",
                    "SUBSCRIBE_FAILED", "REMOTE_USER_IS_NOT_PUBLISHED", "UNEXPECTED_RESPONSE", "WEB_SECURITY_RESTRICT",
                    "PERMISSION_DENIED", "ICE_FAILED", "DISCONNECTED", "CSP_SCRIPT", "CSP_WORKER", "CSP_CONNECT",
                    "CSP_MEDIA", "CSP_OTHER", "DECRYPTION_FAILED", "sdk_error"};
                auto stage = data.contains("stage") && data["stage"].is_string() ? data["stage"].get<std::string>() : "unknown";
                auto reason = data.contains("reason") && data["reason"].is_string() ? data["reason"].get<std::string>() : "sdk_error";
                if (stages.count(stage) == 0) stage = "unknown";
                if (reasons.count(reason) == 0) reason = "sdk_error";
                s.error_after_stop = wxString::Format(_L("Camera playback failed (%s / %s). Try Start camera again."),
                    wxString::FromUTF8(stage), wxString::FromUTF8(reason));
                message(s.error_after_stop);
            }
        });
    }
    auto* buttons = new wxBoxSizer(wxHORIZONTAL);
    s.start = new wxButton(s.display, wxID_ANY, _L("Start camera"));
    s.stop = new wxButton(s.display, wxID_ANY, _L("Stop camera"));
    s.full = new wxButton(s.display, wxID_ANY, _L("Fullscreen"));
    for (auto* button : {s.start, s.stop, s.full}) buttons->Add(button, 0, wxALL, FromDIP(4));
    layout->Add(buttons, 0, wxEXPAND);
    s.status = new wxStaticText(s.display, wxID_ANY, s.viewer ? _L("Camera stopped.") : _L("Camera viewer is unavailable."),
        wxDefaultPosition, FromDIP(wxSize(160, 20)), wxST_ELLIPSIZE_END | wxST_NO_AUTORESIZE);
    s.status->SetMinSize(FromDIP(wxSize(160, 20)));
    layout->Add(s.status, 0, wxEXPAND | wxALL, FromDIP(4));
    s.display->SetSizer(layout);
    box->Add(s.display, 1, wxEXPAND | wxALL, FromDIP(8));
    SetSizer(box);
    box->SetSizeHints(this);
    s.start->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { start(); });
    s.stop->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { stop(); });
    s.full->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) { fullscreen(m_state->frame == nullptr); });
    s.timer.SetOwner(this);
    Bind(wxEVT_TIMER, [this](wxTimerEvent&) { poll(); }, s.timer.GetId());
    s.timer.Start(200);
    poll();
}

AnycubicCloudCamera::~AnycubicCloudCamera()
{
    m_state->closed = true;
    m_state->timer.Stop();
    DeletePendingEvents();
    if (m_state->viewer) {
        m_state->viewer->SetEvtHandlerEnabled(false);
        m_state->viewer->DeletePendingEvents();
    }
    fullscreen(false);
    if (m_state->task) m_state->task->cancel();
}

void AnycubicCloudCamera::message(const wxString& text)
{
    if (m_state->status->GetLabel() != text) {
        m_state->status->SetLabel(text);
        m_state->status->SetToolTip(text);
    }
}

void AnycubicCloudCamera::stop_viewer()
{
    auto& s = *m_state;
    ++s.generation;
    if (s.viewer && s.ready)
        // Do not use the application's script helper: developer mode logs its
        // source. Only this private viewer ever receives short-lived join data.
        s.viewer->RunScriptAsync(wxString::Format("window.orcaCamera.stop(%u);", s.generation));
    s.active = false;
}

void AnycubicCloudCamera::stop()
{
    auto& s = *m_state;
    s.pending = false;
    s.error_after_stop.clear();
    stop_viewer();
    if (s.task) s.task->cancel();
    message(s.task ? _L("Stopping camera...") : _L("Camera stopped."));
}

void AnycubicCloudCamera::start()
{
    auto& s = *m_state;
    const auto selected = s.printer();
    if (s.task || s.pending || !s.viewer || !s.signed_in() || !selected.contains("id")) return;
    s.printer_id = selected["id"];
    ++s.generation;
    s.pending = true;
    s.error_after_stop.clear();
    if (s.ready) begin_request();
    else {
        s.requested = true;
        s.loading_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(20);
        message(_L("Loading camera viewer..."));
        s.viewer->LoadURL(s.url);
    }
}

void AnycubicCloudCamera::begin_request()
{
    auto& s = *m_state;
    if (!s.pending || s.task || !IsShownOnScreen() || !s.signed_in() ||
        s.printer().value("id", json()) != s.printer_id) return;
    const auto previous = camera_owner.lock();
    if (previous && !previous->ready()) {
        s.pending = false;
        message(_L("Waiting for the previous camera session to close."));
        return;
    }
    s.pending = false;
    const auto wanted = s.printer_id;
    s.task = AnycubicCloudReadTask::start_stream([wanted](AnycubicCloudService::CancelFn cancelled,
                                                       AnycubicCloudService::UpdateFn update) {
        AnycubicCloudService service;
        return service.request("camera", {{"printer_id", wanted}, {"confirmed", true}}, {},
                               std::move(cancelled), std::move(update));
    });
    camera_owner = s.task;
    message(_L("Opening camera..."));
}

void AnycubicCloudCamera::poll()
{
    auto& s = *m_state;
    if (s.closed) return;
    const auto selected = s.printer();
    auto* top = dynamic_cast<wxTopLevelWindow*>(wxGetTopLevelParent(this));
    if ((s.pending || s.task) && (!IsShownOnScreen() || (top && top->IsIconized()) ||
        !s.signed_in() || selected.value("id", json()) != s.printer_id)) {
        if (s.pending || (s.task && !s.task->cancelled())) stop();
        fullscreen(false);
    }
    if (s.pending && !s.ready && std::chrono::steady_clock::now() >= s.loading_deadline) {
        stop();
        message(_L("Camera viewer could not be loaded."));
    }
    if (s.task) {
        json frame;
        if (!s.task->cancelled() && s.task->take_update(frame) && frame.contains("viewer") && frame["viewer"].is_object()) {
            s.active = true;
            const auto script = "window.orcaCamera.start(" + frame["viewer"].dump() + "," + std::to_string(s.generation) + ");";
            s.viewer->RunScriptAsync(wxString::FromUTF8(script));
            message(_L("Camera connected; waiting for video frames..."));
        }
        if (s.task->ready()) {
            const auto result = s.task->result();
            const bool cancelled = s.task->cancelled();
            s.task.reset();
            stop_viewer();
            if (result.cleanup_pending) message(_L("Camera closed locally; cloud session cleanup could not be confirmed."));
            else if (!cancelled && !result.success) message(result.error == "camera_open_unknown" ?
                _L("The camera request could not be confirmed. Check Anycubic before retrying.") :
                _L("Camera could not be opened. Close other camera viewers and try again."));
            else message(s.error_after_stop.empty() ? _L("Camera stopped.") : s.error_after_stop);
        }
    }
    const auto previous = camera_owner.lock();
    const bool other_busy = previous && previous != s.task && !previous->ready();
    s.start->Enable(s.viewer && !s.pending && !s.task && !other_busy && s.signed_in() && selected.contains("id"));
    s.stop->Enable(s.pending || bool(s.task));
    s.full->Enable(s.active || s.frame);
}

void AnycubicCloudCamera::fullscreen(bool enter)
{
    auto& s = *m_state;
    if (!enter && s.frame) {
        auto* frame = s.frame;
        s.frame = nullptr;
        frame->GetSizer()->Detach(s.display);
        s.display->Reparent(this);
        s.outer->Add(s.display, 1, wxEXPAND | wxALL, FromDIP(8));
        s.full->SetLabel(_L("Fullscreen"));
        Layout();
        if (GetParent() && !s.closed) GetParent()->Layout();
        frame->SetEvtHandlerEnabled(false);
        frame->DeletePendingEvents();
        frame->Destroy();
    } else if (enter && !s.frame && s.active) {
        auto* frame = new wxFrame(this, wxID_ANY, _L("Anycubic camera — Esc to return"));
        s.frame = frame;
        s.outer->Detach(s.display);
        s.display->Reparent(frame);
        auto* layout = new wxBoxSizer(wxVERTICAL);
        layout->Add(s.display, 1, wxEXPAND);
        frame->SetSizerAndFit(layout);
        frame->Bind(wxEVT_CLOSE_WINDOW, [this](wxCloseEvent&) { fullscreen(false); });
        frame->Bind(wxEVT_CHAR_HOOK, [this](wxKeyEvent& e) {
            if (e.GetKeyCode() == WXK_ESCAPE) fullscreen(false); else e.Skip();
        });
        s.full->SetLabel(_L("Exit fullscreen"));
        frame->ShowFullScreen(true);
        s.viewer->SetFocus();
    }
}

} // namespace Slic3r::GUI
