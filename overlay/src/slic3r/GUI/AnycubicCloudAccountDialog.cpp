#include "AnycubicCloudAccountDialog.hpp"
#include "AnycubicCloudCamera.hpp"
#include "AnycubicFilamentMatch.hpp"

#include "I18N.hpp"
#include "GUI_App.hpp"
#include "Plater.hpp"
#include "PartPlate.hpp"
#include "BitmapComboBox.hpp"
#include "Tab.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Config.hpp"
#include "libslic3r_version.h"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "slic3r/Utils/AnycubicCloudService.hpp"
#include "slic3r/Utils/AnycubicCloudReadTask.hpp"
#include <array>
#include <algorithm>
#include <wx/datetime.h>
#include <wx/font.h>
#include <wx/gauge.h>
#include <wx/wupdlock.h>
#include <wx/window.h>
#include <wx/statbox.h>
#include <wx/timer.h>
#include <atomic>
#include <chrono>
#include <future>
#include <memory>
#include <cmath>
#include <cstddef>
#include <vector>
#include <utility>
#include <map>
#include <wx/choice.h>
#include <wx/checkbox.h>
#include <wx/arrstr.h>
#include <wx/choicdlg.h>
#include <wx/bitmap.h>
#include <wx/brush.h>
#include <wx/colour.h>
#include <wx/dcmemory.h>
#include <wx/pen.h>
#include <wx/gdicmn.h>
#include <wx/scrolwin.h>
#include <wx/textctrl.h>
#include <wx/spinctrl.h>
#include <string>
#include <nlohmann/json.hpp>
#include <wx/button.h>
#include <wx/event.h>
#include <wx/dialog.h>
#include <wx/filedlg.h>
#include <wx/dirdlg.h>
#include <wx/image.h>
#include <wx/mstream.h>
#include <wx/base64.h>
#include <wx/utils.h>
#include <wx/log.h>
#include <wx/listbox.h>
#include <wx/listctrl.h>
#include <wx/msgdlg.h>
#include <wx/progdlg.h>
#include <wx/sizer.h>
#include <wx/stattext.h>
#include <wx/statbmp.h>
#include "libslic3r/AppConfig.hpp"
#include <wx/stdpaths.h>
#include <wx/string.h>

namespace Slic3r::GUI {
namespace {
struct WorkbenchRefreshState {
    // One explicit command at a time. Never cancel/replay a submitted move on view changes.
    std::shared_ptr<AnycubicCloudReadTask> motion;
    nlohmann::json motion_printer;
    std::shared_ptr<AnycubicCloudReadTask> active;
    std::chrono::steady_clock::time_point next;
    nlohmann::json selected_id;
    bool closed{false};
    std::shared_ptr<AnycubicCloudReadTask> monitor;
    std::chrono::steady_clock::time_point monitor_next;
    nlohmann::json monitor_printer;
    nlohmann::json live = nlohmann::json::object();
    std::chrono::steady_clock::time_point last_live_update;
    std::shared_ptr<AnycubicCloudReadTask> light_status;
    nlohmann::json light_status_printer;
    std::chrono::steady_clock::time_point light_status_next;
    nlohmann::json light_command = nlohmann::json::object();
    std::chrono::steady_clock::time_point light_command_hold;
    std::chrono::steady_clock::time_point cloud_received;
    std::shared_ptr<AnycubicCloudReadTask> preview;
    nlohmann::json preview_job;
    nlohmann::json preview_printer;
    bool preview_loaded{false};
    nlohmann::json visible_job;
    bool monitor_enabled{true};
    nlohmann::json rendered;
    std::map<wxStaticText*, std::pair<wxString, int>> labels;
    std::array<std::string, 4> swatch_colors;
    std::array<bool, 4> swatch_initialized{};
    wxString layer_tooltip;
};
AnycubicCloudResult run_request(wxWindow* parent, AnycubicCloudService& service,
                               const std::string& method, const nlohmann::json& arguments = nlohmann::json::object())
{
    std::atomic<bool> cancelled{false};
    auto result = std::async(std::launch::async, [&]() {
        return service.request(method, arguments, {}, [&]() { return cancelled.load(); });
    });
    const wxString message = method == "review_update" ? _L("Checking local update package...") :
                             method == "extract_update" ? _L("Preparing update in a new folder...") :
                             method == "import_session" ? _L("Importing saved sign-in...") :
                             method == "start_print" ? _L("Sending the confirmed print command...") :
                             (method == "upload_gcode" || method == "prepare_print") ?
                             _L("Uploading G-code to Anycubic Cloud...") : _L("Contacting Anycubic Cloud...");
    wxProgressDialog progress(_L("Anycubic Cloud"), message, 100, parent,
                              wxPD_APP_MODAL | wxPD_ELAPSED_TIME |
                              ((method == "start_print" || method == "set_temperature" || method == "peripheral_control" || method == "execute_job_control") ? 0 : wxPD_CAN_ABORT));
    while (result.wait_for(std::chrono::milliseconds(30)) != std::future_status::ready) {
        if (!progress.Pulse())
            cancelled = true;
    }
    return result.get();
}

void show_cloud_library(wxWindow* parent, AnycubicCloudService& service)
{
    wxDialog dialog(parent, wxID_ANY, _L("Anycubic Cloud library"), wxDefaultPosition, wxDefaultSize,
                    wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER);
    auto* layout = new wxBoxSizer(wxVERTICAL);
    auto* note = new wxStaticText(&dialog, wxID_ANY,
        _L("Read-only cloud library. Refresh loads one page of metadata. Files are not opened or downloaded."));
    note->Wrap(dialog.FromDIP(720));
    layout->Add(note, 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    auto* files = new wxListCtrl(&dialog, wxID_ANY, wxDefaultPosition, wxDefaultSize, wxLC_REPORT | wxLC_SINGLE_SEL);
    files->SetMinSize(dialog.FromDIP(wxSize(720, 300)));
    files->AppendColumn(_L("File name"), wxLIST_FORMAT_LEFT, dialog.FromDIP(300));
    files->AppendColumn(_L("File size"), wxLIST_FORMAT_LEFT, dialog.FromDIP(100));
    files->AppendColumn(_L("Uploaded"), wxLIST_FORMAT_LEFT, dialog.FromDIP(220));
    files->AppendColumn(_L("File type"), wxLIST_FORMAT_LEFT, dialog.FromDIP(100));
    layout->Add(files, 1, wxEXPAND | wxLEFT | wxRIGHT, dialog.FromDIP(12));
    auto* status = new wxStaticText(&dialog, wxID_ANY, _L("Cloud library has not been loaded."));
    layout->Add(status, 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    auto* row = new wxBoxSizer(wxHORIZONTAL);
    auto* previous = new wxButton(&dialog, wxID_ANY, _L("Previous page"));
    auto* refresh = new wxButton(&dialog, wxID_ANY, _L("Refresh library"));
    auto* next = new wxButton(&dialog, wxID_ANY, _L("Next page"));
    auto* close = new wxButton(&dialog, wxID_CLOSE, _L("Close"));
    for (auto* button : {previous, refresh, next}) row->Add(button, 0, wxRIGHT, dialog.FromDIP(8));
    row->AddStretchSpacer();
    row->Add(close, 0);
    layout->Add(row, 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    dialog.SetSizerAndFit(layout);
    layout->SetSizeHints(&dialog);
    wxGetApp().UpdateDarkUIWin(&dialog);
    int page = 1;
    bool loaded = false, busy = false, has_next = false;
    auto update_buttons = [&]() {
        previous->Enable(!busy && loaded && page > 1);
        refresh->Enable(!busy);
        next->Enable(!busy && loaded && has_next);
    };
    auto load_page = [&](int wanted) {
        if (busy) return;
        busy = true;
        update_buttons();
        const auto result = run_request(&dialog, service, "cloud_library", {{"page", wanted}});
        busy = false;
        if (result.success) {
            const auto records = result.data.value("files", nlohmann::json::array());
            files->DeleteAllItems();
            long index = 0;
            for (const auto& file : records) {
                const auto name = file.value("name", "");
                files->InsertItem(index, name.empty() ? _L("Unnamed file") : wxString::FromUTF8(name));
                wxString size = _L("Unknown");
                if (file.contains("size") && file["size"].is_number_integer()) {
                    const auto bytes = file["size"].get<unsigned long long>();
                    size = bytes < 1024 ? wxString::FromUTF8(std::to_string(bytes) + " B") :
                        bytes < 1048576 ? wxString::Format("%.2f KiB", double(bytes) / 1024.0) :
                        bytes < 1073741824 ? wxString::Format("%.2f MiB", double(bytes) / 1048576.0) :
                        wxString::Format("%.2f GiB", double(bytes) / 1073741824.0);
                }
                files->SetItem(index, 1, size);
                const auto date = file.value("uploaded_at", "");
                files->SetItem(index, 2, date.empty() ? _L("Unknown") : wxString::FromUTF8(date));
                const auto extension = file.value("extension", "");
                files->SetItem(index, 3, extension.empty() ? _L("Unknown") : wxString::FromUTF8(extension).Upper());
                ++index;
            }
            page = wanted;
            loaded = true;
            has_next = result.data.value("has_next", false);
            status->SetLabel(wxString::Format(_L("Page %d: %d files"), page, int(records.size())));
        } else {
            status->SetLabel(loaded ? _L("Refresh did not complete. Showing the previous results.") :
                                      _L("Could not load the cloud library. Try Refresh."));
        }
        update_buttons();
    };
    previous->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) { load_page(page - 1); });
    refresh->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) { load_page(page); });
    next->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) { load_page(page + 1); });
    close->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) { dialog.EndModal(wxID_CLOSE); });
    update_buttons();
    dialog.CallAfter([&]() { if (dialog.IsShown()) load_page(1); });
    dialog.ShowModal();
}

wxBitmap filament_swatch(wxWindow* owner, const std::string& hex)
{
    const wxSize size = owner->FromDIP(wxSize(24, 16));
    wxBitmap bitmap(size.x, size.y);
    wxMemoryDC dc(bitmap);
    wxColour color;
    const bool known = hex.size() == 7 && color.Set(wxString::FromUTF8(hex));
    dc.SetPen(*wxTRANSPARENT_PEN);
    dc.SetBrush(wxBrush(known ? color : wxColour(150, 150, 150)));
    dc.DrawRectangle(0, 0, size.x, size.y);
    if (!known) {
        dc.SetBrush(wxBrush(wxColour(100, 100, 100)));
        dc.DrawRectangle(0, 0, size.x / 2, size.y / 2);
        dc.DrawRectangle(size.x / 2, size.y / 2, size.x - size.x / 2, size.y - size.y / 2);
    }
    dc.SetPen(wxPen(wxColour(120, 120, 120)));
    dc.SetBrush(*wxTRANSPARENT_BRUSH);
    dc.DrawRectangle(0, 0, size.x, size.y);
    dc.SelectObject(wxNullBitmap);
    return bitmap;
}

wxString cloud_printer_state(const nlohmann::json& printer)
{
    if (!printer.contains("is_printing")) return _L("Unknown");
    const auto& value = printer["is_printing"];
    if (value == 1 || value == "1") return _L("Idle");
    if (value == 2 || value == "2") return _L("Busy");
    return _L("Unknown");
}
} // namespace

static bool sync_cloud_filaments(wxWindow* parent, AnycubicCloudService& service, const nlohmann::json& printer_id);
static wxString print_failure_message(const AnycubicCloudResult& result);

void show_anycubic_cloud_updates(wxWindow* parent)
{
    wxDialog dialog(parent, wxID_ANY, _L("Private build updates"));
    auto* layout = new wxBoxSizer(wxVERTICAL);
    auto* note = new wxStaticText(&dialog, wxID_ANY,
        _L("Select a private OrcaCubic Cloud portable ZIP from a trusted source. The package will be checked and extracted into a NEW folder. Your running application, saved sign-in and profiles are not replaced. A checksum checks integrity, not the publisher's identity. Standard OrcaSlicer releases are incompatible."));
    note->Wrap(dialog.FromDIP(600));
    layout->Add(note, 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    auto* select = new wxButton(&dialog, wxID_ANY, _L("Choose portable update ZIP..."));
    layout->Add(select, 0, wxALL, dialog.FromDIP(12));
    auto* close = new wxButton(&dialog, wxID_CANCEL, _L("Close"));
    layout->Add(close, 0, wxALIGN_RIGHT | wxALL, dialog.FromDIP(12));
    dialog.SetSizerAndFit(layout);
    select->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) {
        wxFileDialog file(&dialog, _L("Choose portable update ZIP"), wxEmptyString, wxEmptyString,
                          _L("ZIP files (*.zip)|*.zip"), wxFD_OPEN | wxFD_FILE_MUST_EXIST);
        if (file.ShowModal() != wxID_OK) return;
        AnycubicCloudService service;
        const std::string path(file.GetPath().ToUTF8().data());
        const auto review = run_request(&dialog, service, "review_update", {{"path", path}});
        if (!review.success) {
            wxMessageBox(_L("This package is damaged or incompatible with this private edition."),
                         _L("Private build updates"), wxOK | wxICON_ERROR, &dialog);
            return;
        }
        const auto question = wxString::Format(
            _L("Prepare %s in a new folder?\n\nSHA-256: %s\n\nContinue only if you trust the package source. The new application will not be started automatically."),
            wxString::FromUTF8(review.data.value("revision", "")), wxString::FromUTF8(review.data.value("sha256", "")));
        if (wxMessageBox(question, _L("Private build updates"), wxYES_NO | wxNO_DEFAULT | wxICON_INFORMATION, &dialog) != wxYES) return;
        wxDirDialog destination(&dialog, _L("Choose the parent folder for the new version"));
        if (destination.ShowModal() != wxID_OK) return;
        const auto result = run_request(&dialog, service, "extract_update", {{"path",path},
            {"output_parent",std::string(destination.GetPath().ToUTF8().data())},{"sha256",review.data.value("sha256","")},{"confirmed",true}});
        if (!result.success) {
            wxMessageBox(_L("The update could not be prepared. The current application and profile were not replaced. An incomplete new folder may remain."),
                _L("Private build updates"), wxOK | wxICON_WARNING, &dialog);
            return;
        }
        wxMessageBox(_L("The new version is ready in its own folder. Save your project and close the old application before starting orca-slicer.exe there. The same OrcaCubicCloudDev profile will be used."),
            _L("Private build updates"), wxOK | wxICON_INFORMATION, &dialog);
        wxLaunchDefaultApplication(wxString::FromUTF8(result.data.value("folder", "")));
    });
    wxGetApp().UpdateDarkUIWin(&dialog);
    dialog.ShowModal();
}

bool can_use_anycubic_cloud_filament_sync()
{
    if (!ORCA_CUSTOM_BUILD || !wxGetApp().preset_bundle) return false;
    const auto& config = wxGetApp().preset_bundle->printers.get_edited_preset().config;
    const auto* model = config.option<ConfigOptionString>("printer_model");
    return model && model->value == "Anycubic Kobra X";
}

wxWindow* create_anycubic_cloud_workbench(wxWindow* parent)
{
    auto* panel = new wxScrolledWindow(parent, wxID_ANY);
    panel->SetScrollRate(panel->FromDIP(8), panel->FromDIP(8));
    const bool live_available = dynamic_cast<wxDialog*>(wxGetTopLevelParent(panel)) == nullptr;
    auto* layout = new wxBoxSizer(wxVERTICAL);
    auto* heading = new wxStaticText(panel, wxID_ANY, _L("Anycubic Workbench"));
    heading->SetFont(heading->GetFont().Bold().Larger());
    layout->Add(heading, 0, wxEXPAND | wxALL, panel->FromDIP(8));
    auto* body = new wxBoxSizer(wxHORIZONTAL);
    auto* sidebar = new wxBoxSizer(wxVERTICAL);
    auto* selector = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Cloud printers"));
    auto* printers = new wxChoice(panel, wxID_ANY);
    printers->SetMinSize(panel->FromDIP(wxSize(180, -1)));
    printers->SetMaxSize(panel->FromDIP(wxSize(180, -1)));
    selector->Add(printers, 0, wxEXPAND | wxALL, panel->FromDIP(6));
    sidebar->Add(selector, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(6));
    auto clipped = [&](const wxString& text, int width) {
        auto* label = new wxStaticText(panel, wxID_ANY, text, wxDefaultPosition,
            panel->FromDIP(wxSize(width, 20)), wxST_ELLIPSIZE_END | wxST_NO_AUTORESIZE);
        label->SetMinSize(panel->FromDIP(wxSize(60, 20)));
        label->SetMaxSize(panel->FromDIP(wxSize(width, 20)));
        return label;
    };
    auto* device = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Printer"));
    auto* name = clipped(_L("Select a cloud printer"), 175);
    name->SetFont(name->GetFont().Bold());
    device->Add(name, 0, wxEXPAND | wxALL, panel->FromDIP(6));
    auto* info = new wxFlexGridSizer(2, panel->FromDIP(4), panel->FromDIP(6));
    info->AddGrowableCol(1);
    const std::array<wxString, 4> labels = {_L("Model"), _L("Connection"), _L("Print state"), _L("Firmware")};
    std::array<wxStaticText*, 4> values{};
    for (std::size_t index = 0; index < values.size(); ++index) {
        info->Add(new wxStaticText(panel, wxID_ANY, labels[index]), 0, wxALIGN_CENTER_VERTICAL);
        values[index] = clipped(_L("Unknown"), 110);
        info->Add(values[index], 1, wxEXPAND);
    }
    device->Add(info, 0, wxEXPAND | wxALL, panel->FromDIP(6));
    sidebar->Add(device, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(6));
    auto* freshness = clipped(_L("Not refreshed"), 180);
    sidebar->Add(freshness, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(6));
    sidebar->AddStretchSpacer();
    auto* account = clipped(_L("No saved Anycubic sign-in."), 180);
    sidebar->Add(account, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(6));
    auto* refresh = new wxButton(panel, wxID_ANY, _L("Refresh printers"));
    auto* import_button = new wxButton(panel, wxID_ANY, _L("Import sign-in..."));
    auto* logout = new wxButton(panel, wxID_ANY, _L("Sign out"));
    for (auto* button : {refresh, import_button, logout})
        sidebar->Add(button, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(4));
    body->Add(sidebar, 0, wxEXPAND | wxRIGHT, panel->FromDIP(8));
    auto* content = new wxBoxSizer(wxVERTICAL);
    auto* right = new wxBoxSizer(wxVERTICAL);
    auto* thermal_row = new wxBoxSizer(wxHORIZONTAL);
    auto* readings = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Printer readings"));
    auto* telemetry_note = new wxStaticText(panel, wxID_ANY, _L("Select a cloud printer"));
    telemetry_note->Hide();
    auto* live_status = clipped(_L("MQTT: waiting for printer selection"), 280);
    auto* live_toggle = new wxButton(panel, wxID_ANY, _L("Stop live monitoring"));
    readings->Add(live_status, 0, wxEXPAND | wxALL, panel->FromDIP(6));
    auto* telemetry_grid = new wxFlexGridSizer(2, panel->FromDIP(4), panel->FromDIP(6));
    telemetry_grid->AddGrowableCol(1);
    const std::array<wxString, 7> telemetry_labels = {
        _L("Nozzle"), _L("Bed"), _L("Current task"),
        _L("Progress"), _L("Remaining"), _L("Layer"), _L("Cloud report time")};
    std::array<wxStaticText*, 7> telemetry_values{};
    auto* layer_progress = new wxGauge(panel, wxID_ANY, 1000);
    layer_progress->SetMinSize(panel->FromDIP(wxSize(120, 10)));
    layer_progress->SetName(_L("Layer progress"));
    layer_progress->Disable();
    for (std::size_t index = 0; index < telemetry_values.size(); ++index) {
        telemetry_values[index] = clipped(_L("Unknown"), 220);
        if (index >= 2 && index <= 5) { telemetry_values[index]->Hide(); continue; }
        telemetry_grid->Add(new wxStaticText(panel, wxID_ANY, telemetry_labels[index]), 0);
        telemetry_grid->Add(telemetry_values[index], 1, wxEXPAND);
    }
    readings->Add(telemetry_grid, 0, wxEXPAND | wxALL, panel->FromDIP(6));
    readings->Add(live_toggle, 0, wxLEFT | wxRIGHT | wxBOTTOM, panel->FromDIP(6));
    thermal_row->Add(readings, 1, wxEXPAND | wxRIGHT, panel->FromDIP(6));
    auto* temperatures = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Temperature control"));
    auto* heater_grid = new wxFlexGridSizer(3, panel->FromDIP(6), panel->FromDIP(4));
    auto* nozzle_target = new wxSpinCtrl(panel, wxID_ANY, "0", wxDefaultPosition, panel->FromDIP(wxSize(65,-1)), wxSP_ARROW_KEYS, 0, 300, 0);
    auto* bed_target = new wxSpinCtrl(panel, wxID_ANY, "0", wxDefaultPosition, panel->FromDIP(wxSize(65,-1)), wxSP_ARROW_KEYS, 0, 100, 0);
    auto* nozzle_apply = new wxButton(panel, wxID_ANY, _L("Set nozzle"));
    auto* bed_apply = new wxButton(panel, wxID_ANY, _L("Set bed"));
    auto* heaters_off = new wxButton(panel, wxID_ANY, _L("Turn both heaters off"));
    nozzle_target->SetToolTip(_L("Nozzle target (0-300 C)"));
    bed_target->SetToolTip(_L("Bed target (0-100 C)"));
    heater_grid->Add(new wxStaticText(panel, wxID_ANY, _L("Nozzle")), 0, wxALIGN_CENTER_VERTICAL);
    heater_grid->Add(nozzle_target, 0);
    heater_grid->Add(nozzle_apply, 0);
    heater_grid->Add(new wxStaticText(panel, wxID_ANY, _L("Bed")), 0, wxALIGN_CENTER_VERTICAL);
    heater_grid->Add(bed_target, 0);
    heater_grid->Add(bed_apply, 0);
    auto* fan_target = new wxSpinCtrl(panel, wxID_ANY, "0", wxDefaultPosition, panel->FromDIP(wxSize(65,-1)), wxSP_ARROW_KEYS, 0, 100, 0);
    auto* fan_apply = new wxButton(panel, wxID_ANY, _L("Set fan"));
    fan_target->SetToolTip(_L("Fan target (0-100 percent)"));
    fan_apply->SetToolTip(_L("Set part cooling fan"));
    heater_grid->Add(new wxStaticText(panel, wxID_ANY, _L("Fan")), 0, wxALIGN_CENTER_VERTICAL);
    heater_grid->Add(fan_target, 0);
    heater_grid->Add(fan_apply, 0);
    temperatures->Add(heater_grid, 0, wxALL, panel->FromDIP(6));
    auto* fan_reading = clipped(_L("Unknown"), 210);
    temperatures->Add(fan_reading, 0, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, panel->FromDIP(6));
    temperatures->Add(heaters_off, 0, wxLEFT | wxRIGHT | wxBOTTOM, panel->FromDIP(6));
    auto* temperature_note = new wxStaticText(panel, wxID_ANY,
        _L("Idle printer only. Enter a target, then confirm the command. Zero turns the selected heater off. Actual temperatures remain in Printer readings."));
    temperature_note->Hide();
    temperatures->GetStaticBox()->SetToolTip(temperature_note->GetLabel());
    thermal_row->Add(temperatures, 0, wxEXPAND);
    content->Add(thermal_row, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(6));
    auto* job_controls = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Current print controls"));
    auto* job_row = new wxBoxSizer(wxHORIZONTAL);
    auto* job_pause = new wxButton(panel, wxID_ANY, _L("Pause print"));
    auto* job_resume = new wxButton(panel, wxID_ANY, _L("Resume print"));
    auto* job_stop = new wxButton(panel, wxID_ANY, _L("Stop print"));
    for (auto* button : {job_pause, job_resume, job_stop}) job_row->Add(button, 0, wxRIGHT, panel->FromDIP(6));
    job_controls->Add(job_row, 0, wxALL, panel->FromDIP(6));
    auto* job_note = new wxStaticText(panel, wxID_ANY,
        _L("Requires a current live task report. Each command checks the cloud job before confirmation and again before sending. Stopping a print cannot be resumed."));
    job_note->Hide();
    job_controls->GetStaticBox()->SetToolTip(job_note->GetLabel());
    content->Add(job_controls, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(6));
    auto* feeder = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Built-in feeder"));
    auto* feeder_grid = new wxFlexGridSizer(2, panel->FromDIP(4), panel->FromDIP(8));
    feeder_grid->AddGrowableCol(0); feeder_grid->AddGrowableCol(1);
    std::array<wxStaticBitmap*, 4> swatches{};
    std::array<wxStaticText*, 4> types{}, materials{};
    for (std::size_t index = 0; index < 4; ++index) {
        auto* row = new wxBoxSizer(wxHORIZONTAL);
        swatches[index] = new wxStaticBitmap(panel, wxID_ANY, filament_swatch(panel, ""));
        row->Add(swatches[index], 0, wxALIGN_CENTER_VERTICAL | wxRIGHT, panel->FromDIP(6));
        auto* text = new wxBoxSizer(wxVERTICAL);
        types[index] = clipped(wxString::Format(_L("Slot %s: %s"), wxString::FromUTF8(std::to_string(index + 1)), _L("Unknown")), 220);
        types[index]->SetFont(types[index]->GetFont().Bold());
        materials[index] = clipped(_L("Feeder data is unavailable."), 220);
        text->Add(types[index], 0, wxEXPAND);
        text->Add(materials[index], 0, wxEXPAND);
        row->Add(text, 1, wxEXPAND);
        feeder_grid->Add(row, 1, wxEXPAND);
    }
    feeder->Add(feeder_grid, 0, wxEXPAND | wxALL, panel->FromDIP(6));
    auto* sync = new wxButton(panel, wxID_ANY, _L("Sync filaments to Prepare"));
    feeder->Add(sync, 0, wxLEFT | wxRIGHT | wxBOTTOM, panel->FromDIP(6));
    content->Add(feeder, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(6));
    auto* plate = new wxButton(panel, wxID_ANY, _L("Current plate: upload / print..."));
    content->Add(plate, 0, wxBOTTOM, panel->FromDIP(6));
    auto* jobs = new wxBoxSizer(wxHORIZONTAL);
    auto* upload = new wxButton(panel, wxID_ANY, _L("Upload G-code to cloud library..."));
    auto* library = new wxButton(panel, wxID_ANY, _L("Browse cloud library..."));
    jobs->Add(upload, 0, wxRIGHT, panel->FromDIP(6));
    jobs->Add(library, 0);
    content->Add(jobs, 0);
    auto* peripherals = new wxBoxSizer(wxHORIZONTAL);
    auto* light_row = new wxBoxSizer(wxHORIZONTAL);
    auto* light_toggle = new wxButton(panel, wxID_ANY, _L("Light on"));
    light_row->Add(light_toggle, 0, wxRIGHT, panel->FromDIP(6));
    peripherals->Add(light_row, 0, wxALL, panel->FromDIP(6));
    auto* peripheral_readings = clipped(_L("Waiting for fan and light reports."), 500);
    peripherals->Add(peripheral_readings, 1, wxALIGN_CENTER_VERTICAL | wxLEFT | wxRIGHT, panel->FromDIP(6));
    auto* peripheral_note = new wxStaticText(panel, wxID_ANY,
        _L("Light status refreshes automatically. Click the light button to switch it immediately."));
    peripheral_note->Hide();
    light_toggle->SetToolTip(peripheral_note->GetLabel());
    auto* overview = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Current print"));
    auto* overview_row = new wxBoxSizer(wxHORIZONTAL);
    wxBitmap preview_blank(panel->FromDIP(64), panel->FromDIP(64));
    {
        wxMemoryDC dc(preview_blank);
        dc.SetBackground(wxBrush(wxColour(45, 48, 52)));
        dc.Clear(); dc.SelectObject(wxNullBitmap);
    }
    auto* job_preview = new wxStaticBitmap(panel, wxID_ANY, preview_blank);
    job_preview->SetMinSize(panel->FromDIP(wxSize(64,64)));
    job_preview->SetToolTip(_L("Preview unavailable"));
    overview_row->Add(job_preview, 0, wxALIGN_CENTER_VERTICAL | wxRIGHT, panel->FromDIP(6));
    auto* overview_text = new wxBoxSizer(wxVERTICAL);
    auto* job_title = clipped(_L("No active print"), 500);
    job_title->SetFont(job_title->GetFont().Bold());
    auto* job_stats = clipped(_L("Waiting for printer data"), 500);
    auto* job_progress = new wxGauge(panel, wxID_ANY, 1000);
    job_progress->SetMinSize(panel->FromDIP(wxSize(120,8)));
    job_progress->SetName(_L("Print progress"));
    auto* link_status = clipped(_L("Connecting..."), 500);
    overview_text->Add(job_title, 0, wxEXPAND);
    overview_text->Add(job_stats, 0, wxEXPAND);
    overview_text->Add(job_progress, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(3));
    overview_text->Add(layer_progress, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(3));
    overview_text->Add(link_status, 0, wxEXPAND);
    overview_row->Add(overview_text, 1, wxEXPAND);
    overview->Add(overview_row, 0, wxEXPAND | wxALL, panel->FromDIP(6));
    auto* motion_box = new wxStaticBoxSizer(wxVERTICAL, panel, _L("Axis movement"));
    auto* motion_row = new wxBoxSizer(wxHORIZONTAL);
    auto* motion_step = new wxChoice(panel, wxID_ANY);
    for (const auto* step : {"1 mm", "5 mm", "10 mm"}) motion_step->Append(step);
    motion_step->SetSelection(0);
    motion_box->Add(motion_step, 0, wxALL, panel->FromDIP(4));
    auto* xy = new wxGridSizer(3, 3, panel->FromDIP(3), panel->FromDIP(3));
    auto* x_minus = new wxButton(panel, wxID_ANY, "X -");
    auto* x_plus = new wxButton(panel, wxID_ANY, "X +");
    auto* y_minus = new wxButton(panel, wxID_ANY, "Y -");
    auto* y_plus = new wxButton(panel, wxID_ANY, "Y +");
    auto* home_all = new wxButton(panel, wxID_ANY, _L("Home"));
    home_all->SetToolTip(_L("Home all axes"));
    xy->AddSpacer(1); xy->Add(y_plus, 0, wxEXPAND); xy->AddSpacer(1);
    xy->Add(x_minus, 0, wxEXPAND); xy->Add(home_all, 0, wxEXPAND); xy->Add(x_plus, 0, wxEXPAND);
    xy->AddSpacer(1); xy->Add(y_minus, 0, wxEXPAND); xy->AddSpacer(1);
    motion_row->Add(xy, 1, wxEXPAND | wxRIGHT, panel->FromDIP(6));
    auto* z_column = new wxBoxSizer(wxVERTICAL);
    auto* z_plus = new wxButton(panel, wxID_ANY, "Z +");
    auto* home_z = new wxButton(panel, wxID_ANY, _L("Home Z"));
    auto* z_minus = new wxButton(panel, wxID_ANY, "Z -");
    for (auto* button : {z_plus, home_z, z_minus}) z_column->Add(button, 0, wxEXPAND | wxBOTTOM, panel->FromDIP(3));
    motion_row->Add(z_column, 0, wxRIGHT, panel->FromDIP(6));
    auto* extruder_column = new wxBoxSizer(wxVERTICAL);
    extruder_column->Add(new wxStaticText(panel, wxID_ANY, _L("Extruder")), 0, wxBOTTOM, panel->FromDIP(3));
    for (const auto& label : {_L("Feed"), _L("Retract")}) {
        auto* button = new wxButton(panel, wxID_ANY, label);
        button->Disable();
        button->SetToolTip(_L("Extruder cloud controls need a verified stop operation for Kobra X."));
        extruder_column->Add(button, 0, wxBOTTOM, panel->FromDIP(3));
    }
    motion_row->Add(extruder_column, 0);
    motion_box->Add(motion_row, 0, wxEXPAND | wxALL, panel->FromDIP(4));
    auto* motion_note = clipped(_L("Idle printer only. Home first; check nozzle clearance."), 500);
    motion_box->Add(motion_note, 0, wxEXPAND | wxALL, panel->FromDIP(4));
    motion_box->GetStaticBox()->SetToolTip(_L("Single finite moves in machine coordinates. No continuous jogging or automatic repeats. Extruder controls are not available yet."));
    auto* note = new wxStaticText(panel, wxID_ANY,
        _L("Live monitoring receives MQTT reports while Device is visible. Values marked live expire after 60 seconds without a fresh report; other available values are marked cloud. Cloud metadata refreshes every 30 seconds. Monitoring does not send printer commands. Camera starts only when you press Start camera. Printer storage browsing is not available. Use Browse cloud library for cloud files."));
    note->Hide();
    heading->SetToolTip(note->GetLabel());
    body->Add(content, 1, wxEXPAND);
    if (live_available) {
        right->Add(overview, 0, wxEXPAND | wxTOP, panel->FromDIP(6));
        right->Add(peripherals, 0, wxEXPAND | wxTOP, panel->FromDIP(6));
        right->Add(motion_box, 0, wxEXPAND | wxTOP, panel->FromDIP(6));
        body->Add(right, 1, wxEXPAND | wxLEFT, panel->FromDIP(8));
    } else {
        content->Add(overview, 0, wxEXPAND | wxTOP, panel->FromDIP(6));
        content->Add(peripherals, 0, wxEXPAND | wxTOP, panel->FromDIP(6));
        content->Add(motion_box, 0, wxEXPAND | wxTOP, panel->FromDIP(6));
    }
    layout->Add(body, 1, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, panel->FromDIP(8));
    panel->SetSizer(layout);
    layout->SetSizeHints(panel);
    panel->FitInside();
    wxGetApp().UpdateDarkUIWin(panel);

    auto service = std::make_shared<AnycubicCloudService>();
    auto records = std::make_shared<nlohmann::json>(nlohmann::json::array());
    auto saved = std::make_shared<bool>(false);
    library->Bind(wxEVT_UPDATE_UI, [saved](wxUpdateUIEvent& event) { event.Enable(*saved); });
    library->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        if (*saved) show_cloud_library(panel, *service);
    });
    auto email = std::make_shared<std::string>();
    auto refresh_state = std::make_shared<WorkbenchRefreshState>();
    auto refresh_timer = std::make_shared<wxTimer>(panel);
    // Account dialogs share the view, but only Device owns the broker client.
    refresh_state->monitor_enabled = live_available;
    live_toggle->Enable(live_available);
    auto selected_printer = [=]() -> nlohmann::json {
        const int selected = printers->GetSelection();
        return selected != wxNOT_FOUND && std::size_t(selected) < records->size() ?
            (*records)[selected] : nlohmann::json::object();
    };
    auto temperature_busy = std::make_shared<bool>(false);
    struct AxisAction { wxButton* button; const char* axis; const char* direction; };
    const std::array<AxisAction, 8> axis_actions = {{
        {x_minus, "X", "minus"}, {x_plus, "X", "plus"},
        {y_minus, "Y", "minus"}, {y_plus, "Y", "plus"},
        {z_minus, "Z", "minus"}, {z_plus, "Z", "plus"},
        {home_all, "ALL", "home"}, {home_z, "Z", "home"}}};
    for (const auto& axis_action : axis_actions) {
        axis_action.button->Bind(wxEVT_UPDATE_UI, [=](wxUpdateUIEvent& event) {
            const auto printer = selected_printer();
            const auto online = printer.value("device_status", nlohmann::json());
            const auto idle = printer.value("is_printing", nlohmann::json());
            const auto model = wxString::FromUTF8(printer.value("model", ""));
            event.Enable(live_available && *saved && !*temperature_busy && printer.contains("id") &&
                (online == 1 || online == "1") && (idle == 1 || idle == "1") &&
                (model.CmpNoCase("Kobra X") == 0 || model.CmpNoCase("Anycubic Kobra X") == 0));
        });
        axis_action.button->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
            if (!*saved || *temperature_busy) return;
            const auto printer = selected_printer();
            const auto wanted = printer.value("id", nlohmann::json());
            if (wanted.is_null()) return;
            const bool home = std::string(axis_action.direction) == "home";
            const std::array<int, 3> distances = {1, 5, 10};
            const int index = motion_step->GetSelection();
            if (index < 0 || index >= int(distances.size())) return;
            const int distance = home ? 0 : distances[index];
            *temperature_busy = true;
            refresh_state->motion_printer = wanted;
            const nlohmann::json arguments = {{"printer_id", wanted},
                {"axis", axis_action.axis}, {"direction", axis_action.direction},
                {"distance", distance}, {"confirmed", true}};
            // The button click authorizes this single finite move. The worker owns
            // only immutable arguments; it never captures the panel or selection.
            refresh_state->motion = AnycubicCloudReadTask::start(
                [arguments](AnycubicCloudService::CancelFn) {
                    AnycubicCloudService sender;
                    return sender.request("motion_control", arguments);
                });
            motion_note->SetLabel(_L("Contacting Anycubic Cloud..."));
            motion_note->SetToolTip(motion_note->GetLabel());
            for (const auto& action : axis_actions) action.button->Disable();
        });
    }
    auto current_live_job = [=]() -> nlohmann::json {
        if (!live_available || !*saved || !refresh_state->monitor_enabled || !refresh_state->monitor ||
            refresh_state->monitor->cancelled() || refresh_state->monitor->ready() ||
            refresh_state->monitor_printer != selected_printer().value("id", nlohmann::json()) ||
            !refresh_state->live.value("connected", false) ||
            std::chrono::steady_clock::now() - refresh_state->last_live_update > std::chrono::seconds(3))
            return nlohmann::json::object();
        const auto job = refresh_state->live.value("control_job", nlohmann::json::object());
        if (!job.is_object() || !job.contains("project_id") || !job["project_id"].is_number_integer())
            return nlohmann::json::object();
        const auto phase = job.value("phase", "");
        return phase == "printing" || phase == "paused" ? job : nlohmann::json::object();
    };
    for (auto* button : {job_pause, job_resume, job_stop}) {
        button->Bind(wxEVT_UPDATE_UI, [=](wxUpdateUIEvent& event) {
            const auto job = current_live_job();
            const auto printer = selected_printer();
            const auto online = printer.value("device_status", nlohmann::json());
            const auto busy = printer.value("is_printing", nlohmann::json());
            event.Enable(!*temperature_busy && !job.empty() && (online == 1 || online == "1") &&
                (busy == 2 || busy == "2") && (button == job_stop ||
                job.value("phase", "") == (button == job_pause ? "printing" : "paused")));
        });
        button->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
            if (*temperature_busy) return;
            const auto job = current_live_job();
            const auto wanted = selected_printer().value("id", nlohmann::json());
            if (job.empty() || wanted.is_null()) return;
            const std::string action = button == job_pause ? "pause" : button == job_resume ? "resume" : "stop";
            *temperature_busy = true;
            const auto plan = run_request(panel, *service, "review_job_control",
                {{"printer_id", wanted}, {"project_id", job["project_id"]}, {"action", action}});
            if (!plan.success) {
                *temperature_busy = false;
                wxMessageBox(_L("The current job could not be verified, or a previous command is still pending. No new control command was sent. Wait for a fresh printer report."),
                             _L("Current print controls"), wxOK | wxICON_WARNING, panel);
                return;
            }
            auto still_current = [=]() {
                return *saved && selected_printer().value("id", nlohmann::json()) == wanted && current_live_job() == job;
            };
            if (!still_current()) { *temperature_busy = false; return; }
            const auto verb = button == job_pause ? _L("Pause print") : button == job_resume ? _L("Resume print") : _L("Stop print");
            const auto printer_name = plan.data.value("printer_name", "");
            const auto job_name = plan.data.value("job_name", "");
            const auto question = wxString::Format(
                _L("%s on %s?\n\nJob: %s\nProject ID: %s\n\nOnly this verified job will be targeted."), verb,
                printer_name.empty() ? _L("Anycubic Kobra X") : wxString::FromUTF8(printer_name),
                job_name.empty() ? _L("Unnamed print") : wxString::FromUTF8(job_name), wxString::FromUTF8(job["project_id"].dump()));
            const auto extra = button == job_stop ? wxString("\n\n") + _L("Stopping a print cannot be resumed.") : wxString();
            if (wxMessageBox(question + extra, _L("Confirm print control"),
                             wxYES_NO | wxNO_DEFAULT | wxICON_WARNING, panel) != wxYES) {
                *temperature_busy = false;
                return;
            }
            if (!still_current()) {
                *temperature_busy = false;
                wxMessageBox(_L("The selected printer or live job changed or expired. No control command was sent."),
                             _L("Current print controls"), wxOK | wxICON_WARNING, panel);
                return;
            }
            const auto result = run_request(panel, *service, "execute_job_control",
                {{"printer_id", wanted}, {"ticket", plan.data["ticket"]}, {"confirmed", true}});
            *temperature_busy = false;
            wxMessageBox(result.success ?
                _L("Cloud accepted the print control. Waiting for the printer's report to confirm its state.") :
                _L("The print control was not confirmed or the job changed. Check the printer; the command may already have reached it. It will not be retried automatically."),
                _L("Current print controls"), wxOK | (result.success ? wxICON_INFORMATION : wxICON_WARNING), panel);
            refresh_state->next = std::chrono::steady_clock::time_point{};
        });
    }
    auto peripheral_fields = [=]() -> nlohmann::json {
        if (!*saved || !refresh_state->monitor_enabled || !refresh_state->monitor ||
            refresh_state->monitor->cancelled() || refresh_state->monitor->ready() ||
            refresh_state->monitor_printer != selected_printer().value("id", nlohmann::json()) ||
            !refresh_state->live.value("connected", false)) return nlohmann::json::object();
        return refresh_state->live.value("fields", nlohmann::json::object());
    };
    auto next_light_value = [=]() {
        const auto fields = peripheral_fields();
        const auto& command = refresh_state->light_command;
        const bool same = command.value("printer_id", nlohmann::json()) == selected_printer().value("id", nlohmann::json()) &&
            command.value("light_type", nlohmann::json()) == fields.value("light_type", nlohmann::json()) && command.contains("value");
        // Keep the requested direction stable while older reports are in flight.
        if (same && std::chrono::steady_clock::now() < refresh_state->light_command_hold)
            return !command["value"].get<bool>();
        if (fields.contains("light_on")) return !fields["light_on"].get<bool>();
        return same ? !command["value"].get<bool>() : true;
    };
    const std::array<wxButton*, 2> peripheral_buttons = {fan_apply, light_toggle};
    for (auto* button : peripheral_buttons) {
        button->Bind(wxEVT_UPDATE_UI, [=](wxUpdateUIEvent& event) {
            const auto printer = selected_printer();
            const auto online = printer.value("device_status", nlohmann::json());
            const auto state = printer.value("is_printing", nlohmann::json());
            const auto model = wxString::FromUTF8(printer.value("model", ""));
            const auto fields = peripheral_fields();
            const bool known_light = fields.contains("light_type");
            if (button == light_toggle) {
                const auto label = next_light_value() ? _L("Light on") : _L("Light off");
                if (button->GetLabel() != label) button->SetLabel(label);
            }
            event.Enable(live_available && *saved && !*temperature_busy && printer.contains("id") &&
                (online == 1 || online == "1") &&
                (state == 1 || state == "1" || (button != fan_apply && (state == 2 || state == "2"))) &&
                (model.CmpNoCase("Kobra X") == 0 || model.CmpNoCase("Anycubic Kobra X") == 0) &&
                (button == fan_apply || (known_light && !refresh_state->light_status)));
        });
        button->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
            if (*temperature_busy || !*saved) return;
            const auto printer = selected_printer();
            if (!printer.contains("id")) return;
            const auto wanted = printer["id"];
            const std::string action = button == fan_apply ? "fan" : "light";
            nlohmann::json args = {{"printer_id", wanted}, {"action", action}, {"confirmed", true}};
            wxString change;
            if (action == "fan") {
                args["value"] = fan_target->GetValue();
                change = wxString::Format(_L("Part cooling fan target: %d percent"), fan_target->GetValue());
            } else if (action == "light") {
                const auto fields = peripheral_fields();
                if (!fields.contains("light_type")) return;
                args["light_type"] = fields["light_type"];
                args["value"] = next_light_value();
            }
            if (action == "fan" && wxMessageBox(wxString::Format(
                _L("Send this command to %s?\n\n%s\n\nThis changes the physical printer."),
                wxString::FromUTF8(printer.value("name", "Anycubic Kobra X")), change),
                _L("Confirm fan or light control"), wxYES_NO | wxNO_DEFAULT | wxICON_WARNING, panel) != wxYES) return;
            if (!*saved || selected_printer().value("id", nlohmann::json()) != wanted) return;
            if (action == "light" && peripheral_fields().value("light_type", nlohmann::json()) != args["light_type"]) {
                return;
            }
            *temperature_busy = true;
            const auto result = run_request(panel, *service, "peripheral_control", args);
            *temperature_busy = false;
            if (action == "light" && result.success && selected_printer().value("id", nlohmann::json()) == wanted) {
                refresh_state->light_command = args;
                refresh_state->light_command_hold = std::chrono::steady_clock::now() + std::chrono::seconds(10);
            }
            if (action == "fan" || !result.success) wxMessageBox(result.success ?
                _L("Cloud accepted the command. Check the live fan or light report to confirm the change.") :
                _L("The command was not confirmed. Check the printer before trying again; it may already have received the command."),
                _L("Fan and light"), wxOK | (result.success ? wxICON_INFORMATION : wxICON_WARNING), panel);
            refresh_state->next = std::chrono::steady_clock::time_point{};
        });
    }
    const std::array<wxButton*, 3> temperature_buttons = {nozzle_apply, bed_apply, heaters_off};
    for (auto* button : temperature_buttons) {
        button->Bind(wxEVT_UPDATE_UI, [=](wxUpdateUIEvent& event) {
            const auto printer = selected_printer();
            const auto online = printer.value("device_status", nlohmann::json());
            const auto idle = printer.value("is_printing", nlohmann::json());
            const auto model = wxString::FromUTF8(printer.value("model", ""));
            event.Enable(live_available && *saved && !*temperature_busy && printer.contains("id") &&
                (online == 1 || online == "1") && (idle == 1 || idle == "1") &&
                (model.CmpNoCase("Kobra X") == 0 || model.CmpNoCase("Anycubic Kobra X") == 0));
        });
        button->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
            if (*temperature_busy) return;
            const auto printer = selected_printer();
            if (!*saved || !printer.contains("id")) return;
            const auto wanted = printer["id"];
            const std::string heater = button == nozzle_apply ? "nozzle" : button == bed_apply ? "bed" : "both";
            const int target = button == nozzle_apply ? nozzle_target->GetValue() :
                               button == bed_apply ? bed_target->GetValue() : 0;
            const wxString change = heater == "both" ? _L("Turn both heaters off") :
                wxString::Format(_L("%s target: %d C"), heater == "nozzle" ? _L("Nozzle") : _L("Bed"), target);
            const auto question = wxString::Format(
                _L("Send this temperature command to %s?\n\n%s\n\nThis changes the physical printer. The printer must be idle."),
                wxString::FromUTF8(printer.value("name", "Anycubic Kobra X")), change);
            if (wxMessageBox(question, _L("Confirm temperature control"),
                             wxYES_NO | wxNO_DEFAULT | wxICON_WARNING, panel) != wxYES) return;
            if (selected_printer().value("id", nlohmann::json()) != wanted || !*saved) return;
            *temperature_busy = true;
            const auto result = run_request(panel, *service, "set_temperature",
                {{"printer_id", wanted}, {"heater", heater}, {"target", target}, {"confirmed", true}});
            *temperature_busy = false;
            wxMessageBox(result.success ?
                _L("Cloud accepted the temperature command. Check the actual printer readings; this does not mean the target has been reached.") :
                _L("The temperature command was not confirmed. It may already have reached the printer. Check its temperatures and idle state before trying again."),
                _L("Temperature control"), wxOK | (result.success ? wxICON_INFORMATION : wxICON_WARNING), panel);
            refresh_state->next = std::chrono::steady_clock::time_point{};
        });
    }
    auto* camera = live_available ? new AnycubicCloudCamera(panel, selected_printer, [saved]() { return *saved; }) : nullptr;
    if (camera) {
        right->Insert(0, camera, 1, wxEXPAND);
        wxGetApp().UpdateDarkUIWin(camera);
        panel->Layout();
        panel->FitInside();
    }
    auto update_details = [=]() {
        const auto printer = selected_printer();
        const auto now = std::chrono::steady_clock::now();
        const auto cloud_age = refresh_state->cloud_received == std::chrono::steady_clock::time_point{} ? -1 :
            int(std::chrono::duration_cast<std::chrono::seconds>(now - refresh_state->cloud_received).count());
        const nlohmann::json render = {{"printer", printer}, {"live", refresh_state->live},
            {"cloud_age", cloud_age},
            {"signed_in", *saved}, {"enabled", refresh_state->monitor_enabled},
            {"monitor_printer", refresh_state->monitor_printer},
            {"cancelled", refresh_state->monitor && refresh_state->monitor->cancelled()},
            {"wrap_width", panel->FromDIP(540)}};
        if (render == refresh_state->rendered) return;
        refresh_state->rendered = render;
        bool relayout = false;
        auto set_text = [&](wxStaticText* control, const wxString& text, int wrap = 0) {
            const auto previous = refresh_state->labels.find(control);
            if (previous != refresh_state->labels.end() && previous->second == std::make_pair(text, wrap)) return;
            refresh_state->labels[control] = {text, wrap};
            const auto before = control->GetBestSize();
            control->SetLabel(text);
            control->SetToolTip(text);
            if (wrap > 0) control->Wrap(wrap);
            relayout = relayout || before != control->GetBestSize();
        };
        std::array<wxString, 4> value_text;
        value_text.fill(_L("Unknown"));
        std::array<wxString, 7> reading_text;
        reading_text.fill(_L("Unknown"));
        set_text(name, printer.empty() ? _L("Select a cloud printer") :
            wxString::FromUTF8(printer.value("name", "Anycubic Printer")));
        const auto model = printer.value("model", "");
        value_text[0] = model.empty() ? _L("Unknown") : wxString::FromUTF8(model);
        const auto connection = printer.value("device_status", nlohmann::json());
        value_text[1] = connection == 1 || connection == "1" ? _L("Online") :
                            connection == 0 || connection == "0" ? _L("Offline") : _L("Unknown");
        value_text[2] = cloud_printer_state(printer);
        const auto firmware = printer.value("firmware_version", "");
        value_text[3] = firmware.empty() ? _L("Unknown") : wxString::FromUTF8(firmware);
        auto telemetry = printer.value("telemetry", nlohmann::json::object());
        auto job = telemetry.value("job", nlohmann::json::object());
        std::array<bool, 6> live_source{};
        const bool have_live = *saved && refresh_state->monitor_enabled && !refresh_state->live.empty() &&
            (!refresh_state->monitor || !refresh_state->monitor->cancelled()) &&
            refresh_state->monitor_printer == printer.value("id", nlohmann::json());
        const auto live = have_live ? refresh_state->live : nlohmann::json::object();
        const auto fields = live.value("fields", nlohmann::json::object());
        const auto fan_text = fields.contains("part_fan") ?
            wxString::FromUTF8(std::to_string(fields["part_fan"].get<int>()) + "%") : _L("Unknown");
        const auto light_text = fields.contains("light_on") ?
            (fields["light_on"].get<bool>() ? _L("On") : _L("Off")) :
            fields.contains("light_type") ? _L("State unknown; light is identified") :
            fields.value("light_report_problem", "") == "ambiguous" ? _L("Multiple lights reported") :
            fields.contains("light_report_problem") ? _L("Unsupported light report") :
            live.value("connected", false) && !live.value("light_reports_subscribed", true) ?
                _L("Light reports unavailable") : _L("Unknown");
        set_text(fan_reading, wxString::Format(_L("Fan: %s"), fan_text));
        set_text(peripheral_readings, wxString::Format(_L("Light: %s"), light_text));
        const bool connected = live.value("connected", false);
        const auto live_error = live.value("error", "");
        const auto live_state = live.value("state", "");
        const auto toggle_text = refresh_state->monitor_enabled ? _L("Stop live monitoring") : _L("Start live monitoring");
        if (live_toggle->GetLabel() != toggle_text) live_toggle->SetLabel(toggle_text);
        set_text(live_status, !live_available ? _L("Open Device for live monitoring.") :
            !refresh_state->monitor_enabled ? _L("MQTT: stopped") :
            !*saved ? _L("MQTT: sign in to Anycubic first") :
            printer.empty() ? _L("MQTT: waiting for printer selection") :
            live_error == "monitor_tls_failed" ? _L("MQTT: TLS verification failed; cloud readings remain available.") :
            live_error == "monitor_auth_rejected" ? _L("MQTT: sign-in rejected; cloud readings remain available.") :
            live_error == "monitor_subscription_rejected" ? _L("MQTT: subscription rejected; cloud readings remain available.") :
            !live_error.empty() ? _L("MQTT: connection failed; retrying in the background.") :
            live_state == "live" ? _L("MQTT: receiving live reports") :
            live_state == "stale" ? _L("MQTT: no fresh printer reports; cloud readings remain available.") :
            connected ? _L("MQTT: connected, waiting for printer reports") : _L("MQTT: connecting..."), panel->FromDIP(540));
        if (connected) {
            if (live.value("job_seen", false)) job = nlohmann::json::object();
            for (std::size_t index = 0; index < 2; ++index) {
                const auto key = index == 0 ? "nozzle" : "bed";
                if (fields.contains(key) && fields[key].is_number()) {
                    telemetry[key] = fields[key];
                    live_source[index] = true;
                }
            }
            for (const auto* key : {"name", "progress", "remaining_minutes", "layer", "layers"})
                if (fields.contains(key)) job[key] = fields[key];
            live_source[2] = fields.contains("name");
            live_source[3] = fields.contains("progress");
            live_source[4] = fields.contains("remaining_minutes");
            live_source[5] = fields.contains("layer") && fields.contains("layers");
            if (!fields.empty()) value_text[1] = wxString::Format(_L("%s (live)"), _L("Online"));
            if (fields.contains("is_printing")) {
                const auto state = fields["is_printing"] == 1 ? _L("Idle") : _L("Busy");
                value_text[2] = wxString::Format(_L("%s (live)"), state);
            }
        }
        set_text(telemetry_note, telemetry.empty() ? _L("Cloud readings have not been loaded for this printer.") :
            !telemetry.value("detail_loaded", false) ? _L("Printer details could not be refreshed. Available values come from the cloud printer list.") :
            (printer.value("is_printing", nlohmann::json()) == 2 ||
             printer.value("is_printing", nlohmann::json()) == "2") && !telemetry.value("projects_loaded", false) ?
                _L("Printer details loaded; cloud task data could not be refreshed.") :
                _L("Cloud snapshot; values may be delayed."), panel->FromDIP(540));
        for (std::size_t index = 0; index < 2; ++index) {
            const auto key = index == 0 ? "nozzle" : "bed";
            if (telemetry.contains(key) && telemetry[key].is_number())
                reading_text[index] = wxString::Format(_L("%s °C"),
                    wxString::Format("%.1f", telemetry[key].get<double>()));
        }
        const auto job_name = job.value("name", "");
        if (!job_name.empty()) reading_text[2] = wxString::FromUTF8(job_name);
        if (job.contains("progress"))
            reading_text[3] = wxString::Format("%.1f%%", job["progress"].get<double>());
        if (job.contains("remaining_minutes"))
            reading_text[4] = wxString::Format(_L("%s min"),
                wxString::Format("%.0f", job["remaining_minutes"].get<double>()));
        int layer_fill = 0;
        bool layer_known = false;
        wxString layer_tooltip = _L("Unknown");
        if (job.contains("layer") && job["layer"].is_number() &&
            job.contains("layers") && job["layers"].is_number()) {
            reading_text[5] = wxString::Format("%.0f / %.0f",
                job["layer"].get<double>(), job["layers"].get<double>());
            const double current_layer = job["layer"].get<double>();
            const double total_layers = job["layers"].get<double>();
            // Layer progress is independent of the cloud's time-based print
            // percentage. Missing/invalid totals must not look like 0% known.
            if (std::isfinite(current_layer) && std::isfinite(total_layers) &&
                current_layer >= 0 && total_layers > 0 && current_layer <= total_layers) {
                const double fraction = current_layer / total_layers;
                layer_known = true;
                layer_fill = int(std::round(fraction * layer_progress->GetRange()));
                layer_tooltip = wxString::Format("%.1f%%", fraction * 100);
            }
        }
        if (refresh_state->layer_tooltip != layer_tooltip) {
            refresh_state->layer_tooltip = layer_tooltip;
            layer_progress->SetToolTip(layer_tooltip);
        }
        if (layer_progress->IsEnabled() != layer_known) layer_progress->Enable(layer_known);
        if (layer_progress->GetValue() != layer_fill) layer_progress->SetValue(layer_fill);
        const auto reported = telemetry.value("reported_at", "");
        if (!reported.empty()) reading_text[6] = wxString::FromUTF8(reported);
        refresh_state->visible_job = job.value("project_id", nlohmann::json());
        if (live.contains("control_job"))
            refresh_state->visible_job = live["control_job"].value("project_id", nlohmann::json());
        set_text(job_title, !job_name.empty() ? wxString::FromUTF8(job_name) :
            (printer.value("is_printing", nlohmann::json()) == 2 || printer.value("is_printing", nlohmann::json()) == "2") ?
                _L("Print name unavailable") : _L("No active print"));
        set_text(job_stats, wxString::Format(_L("Progress: %s | Remaining: %s | Layer: %s"),
            reading_text[3], reading_text[4], reading_text[5]));
        const bool progress_known = job.contains("progress") && job["progress"].is_number();
        if (job_progress->IsEnabled()!=progress_known) job_progress->Enable(progress_known);
        const int print_fill = progress_known ? int(std::round(std::clamp(job["progress"].get<double>(),0.0,100.0)*10)) : 0;
        if (job_progress->GetValue()!=print_fill) job_progress->SetValue(print_fill);
        const auto report_age = live.value("report_age_seconds", -1);
        const auto connection_text = !*saved ? _L("Sign in to receive printer data") :
            connection == 0 || connection == "0" ? _L("Printer offline; displayed data may be delayed") :
            !refresh_state->monitor_enabled ? _L("Live monitoring stopped") :
            connected && live_state=="live" && report_age>=0 ?
                wxString::Format(_L("Live report: %d s ago"), report_age) :
            connected ? _L("Connected; waiting for fresh printer data") :
                _L("Reconnecting; displayed cloud data may be delayed");
        set_text(link_status, cloud_age>=0 ? wxString::Format(_L("%s | Cloud read: %d s ago"),connection_text,cloud_age) : connection_text);
        const std::array<bool, 6> have_value = {
            telemetry.contains("nozzle"), telemetry.contains("bed"), !job_name.empty(),
            job.contains("progress"), job.contains("remaining_minutes"), job.contains("layer") && job.contains("layers")};
        for (std::size_t index = 0; index < live_source.size(); ++index)
            if (have_value[index])
                reading_text[index] = wxString::Format(live_source[index] ? _L("%s (live)") : _L("%s (cloud)"),
                    reading_text[index]);
        for (std::size_t index = 0; index < values.size(); ++index) set_text(values[index], value_text[index]);
        for (std::size_t index = 0; index < telemetry_values.size(); ++index)
            set_text(telemetry_values[index], reading_text[index]);
        for (std::size_t index = 0; index < 4; ++index) {
            nlohmann::json slot = nlohmann::json::object();
            if (printer.contains("boxes"))
                for (const auto& box : printer["boxes"])
                    if (box["id"] == -1)
                        for (const auto& item : box["slots"])
                            if (item["index"] == index) slot = item;
            const auto present = slot.value("present", nlohmann::json());
            const auto type = present == false ? _L("Empty") : present == true ?
                wxString::FromUTF8(slot.value("type", "")) : _L("Unknown");
            set_text(types[index], wxString::Format(_L("Slot %s: %s"),
                wxString::FromUTF8(std::to_string(index + 1)), type));
            const auto material = slot.value("name", "");
            set_text(materials[index], present.is_null() ? _L("Feeder data is unavailable.") :
                present == false ? _L("Empty") : material.empty() ? _L("Material name not reported") : wxString::FromUTF8(material));
            const auto color = present == true ? slot.value("color", "") : "";
            if (!refresh_state->swatch_initialized[index] || refresh_state->swatch_colors[index] != color) {
                refresh_state->swatch_initialized[index] = true;
                refresh_state->swatch_colors[index] = color;
                swatches[index]->SetBitmap(filament_swatch(panel, color));
            }
        }
        if (relayout) {
            panel->Layout();
            panel->FitInside();
        }
    };
    auto clear_snapshot = [=]() {
        refresh_state->cloud_received = std::chrono::steady_clock::time_point{};
        if (refresh_state->preview) refresh_state->preview->cancel();
        records->clear();
        printers->Clear();
        freshness->SetLabel(_L("Not refreshed"));
        update_details();
    };
    auto update_account = [=]() {
        const auto result = service->request("account"); // Local encrypted storage only.
        const bool signed_in = result.success && result.data.value("signed_in", false);
        const auto next_email = result.success ? result.data.value("email", "") : "";
        if (!signed_in || *email != next_email) {
            if (refresh_state->light_status) refresh_state->light_status->cancel();
            refresh_state->light_command = nlohmann::json::object();
            refresh_state->light_status_next = std::chrono::steady_clock::time_point{};
            if (refresh_state->active) refresh_state->active->cancel();
            if (refresh_state->monitor) refresh_state->monitor->cancel();
            refresh_state->live = nlohmann::json::object();
            refresh_state->selected_id = nullptr;
            clear_snapshot();
        }
        *saved = signed_in;
        *email = next_email;
        refresh->Enable(signed_in && !refresh_state->active);
        logout->Enable(signed_in);
        upload->Enable(signed_in);
        account->SetLabel(!result.success ? _L("The saved sign-in could not be read.") : !signed_in ?
            _L("No saved Anycubic sign-in.") : wxString::Format(_L("Saved Anycubic sign-in: %s"), wxString::FromUTF8(next_email)));
        panel->Layout();
    };
    printers->Bind(wxEVT_CHOICE, [=](wxCommandEvent&) {
        if (refresh_state->light_status) refresh_state->light_status->cancel();
        refresh_state->light_command = nlohmann::json::object();
        if (camera) camera->stop();
        refresh_state->selected_id = selected_printer().value("id", nlohmann::json());
        refresh_state->monitor_next = std::chrono::steady_clock::time_point{};
        if (refresh_state->active) refresh_state->active->cancel();
        if (refresh_state->monitor) refresh_state->monitor->cancel();
        refresh_state->live = nlohmann::json::object();
        refresh_state->next = std::chrono::steady_clock::time_point{};
        update_details();
    });
    auto apply_refresh = [=](const AnycubicCloudResult& result) {
        const auto previous = selected_printer().value("id", refresh_state->selected_id);
        if (!result.success) {
            if (result.error != "cancelled") {
                freshness->SetLabel(records->empty() ?
                    _L("Could not refresh printers. Check your Anycubic sign-in and internet connection.") :
                    _L("Refresh failed. Displayed data is the previous cloud snapshot."));
                freshness->SetToolTip(freshness->GetLabel());
                panel->Layout();
            }
            return;
        }
        refresh_state->cloud_received = std::chrono::steady_clock::now();
        auto incoming = nlohmann::json::array();
        if (result.data.contains("printers") && result.data["printers"].is_array()) incoming = result.data["printers"];
        bool rebuild = incoming.size() != records->size();
        for (std::size_t index = 0; !rebuild && index < incoming.size(); ++index)
            rebuild = incoming[index].value("id", nlohmann::json()) != (*records)[index].value("id", nlohmann::json()) ||
                incoming[index].value("name", "Anycubic Printer") != (*records)[index].value("name", "Anycubic Printer");
        *records = std::move(incoming);
        if (rebuild) {
            wxWindowUpdateLocker hold(printers);
            printers->Clear();
            for (const auto& printer : *records)
                printers->Append(wxString::FromUTF8(printer.value("name", "Anycubic Printer")));
        }
        int selected = wxNOT_FOUND;
        for (std::size_t index = 0; index < records->size(); ++index)
            if (!previous.is_null() && (*records)[index].value("id", nlohmann::json()) == previous) selected = int(index);
        if (previous.is_null() && records->size() == 1) selected = 0;
        if (printers->GetSelection() != selected) printers->SetSelection(selected);
        if (selected != wxNOT_FOUND)
            refresh_state->selected_id = (*records)[selected].value("id", nlohmann::json());
        freshness->SetLabel(wxString::Format(_L("Cloud snapshot: %s"), wxDateTime::Now().FormatISOCombined(' ')));
        freshness->SetToolTip(freshness->GetLabel());
        update_details();
    };
    auto start_refresh = [=]() {
        if (!*saved || refresh_state->closed || refresh_state->active || !panel->IsShownOnScreen()) return;
        refresh->Disable();
        const auto wanted = refresh_state->selected_id;
        refresh_state->active = AnycubicCloudReadTask::start([wanted](AnycubicCloudService::CancelFn cancelled) {
            AnycubicCloudService reader;
            return reader.request("workbench_snapshot", {{"printer_id", wanted}}, {}, std::move(cancelled));
        });
    };
    auto start_monitor = [=]() {
        const auto wanted = selected_printer().value("id", nlohmann::json());
        if (!live_available || !refresh_state->monitor_enabled || !*saved || refresh_state->closed ||
            refresh_state->monitor || wanted.is_null() || !panel->IsShownOnScreen() ||
            std::chrono::steady_clock::now() < refresh_state->monitor_next) return;
        refresh_state->monitor_printer = wanted;
        refresh_state->live = nlohmann::json::object();
        refresh_state->monitor = AnycubicCloudReadTask::start_stream(
            [wanted](AnycubicCloudService::CancelFn cancelled, AnycubicCloudService::UpdateFn update) {
                AnycubicCloudService reader;
                return reader.request("monitor", {{"printer_id", wanted}}, {}, std::move(cancelled), std::move(update));
            });
        update_details();
    };
    live_toggle->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        if (refresh_state->light_status) refresh_state->light_status->cancel();
        refresh_state->light_status_next = std::chrono::steady_clock::time_point{};
        refresh_state->monitor_enabled = !refresh_state->monitor_enabled;
        if (refresh_state->monitor) refresh_state->monitor->cancel();
        refresh_state->live = nlohmann::json::object();
        refresh_state->monitor_next = std::chrono::steady_clock::time_point{};
        update_details();
    });
    panel->Bind(wxEVT_TIMER, [=](wxTimerEvent&) {
        if (refresh_state->closed) return;
        const auto motion = refresh_state->motion;
        if (motion && motion->ready()) {
            const auto result = motion->result();
            refresh_state->motion.reset();
            *temperature_busy = false;
            if (*saved && selected_printer().value("id", nlohmann::json()) == refresh_state->motion_printer) {
                motion_note->SetLabel(result.success ?
                    _L("Cloud accepted the move. Wait until the printer finishes before the next move.") :
                    _L("Move not confirmed. Check the printer; no automatic retry."));
                motion_note->SetToolTip(motion_note->GetLabel());
                if (!result.success && panel->IsShownOnScreen())
                    wxMessageBox(motion_note->GetLabel(), _L("Axis movement"), wxOK | wxICON_WARNING, panel);
            } else {
                motion_note->SetLabel(_L("Idle printer only. Home first; check nozzle clearance."));
                motion_note->SetToolTip(motion_note->GetLabel());
            }
            refresh_state->next = std::chrono::steady_clock::time_point{};
        }
        if (!panel->IsShownOnScreen()) {
            if (refresh_state->preview) refresh_state->preview->cancel();
            if (refresh_state->light_status) refresh_state->light_status->cancel();
            if (refresh_state->active) refresh_state->active->cancel();
            if (refresh_state->monitor) refresh_state->monitor->cancel();
            refresh_state->live = nlohmann::json::object();
            return;
        }
        update_details();
        const auto preview_printer = selected_printer().value("id", nlohmann::json());
        const auto preview_job = refresh_state->visible_job;
        if (refresh_state->preview_printer != preview_printer || refresh_state->preview_job != preview_job) {
            if (refresh_state->preview) refresh_state->preview->cancel();
            refresh_state->preview_printer = preview_printer;
            refresh_state->preview_job = preview_job;
            refresh_state->preview_loaded = false;
            job_preview->SetBitmap(preview_blank);
            job_preview->SetToolTip(_L("Preview unavailable"));
        }
        if (refresh_state->preview && refresh_state->preview->ready()) {
            const auto task = refresh_state->preview;
            if (!task->cancelled() && task->result().success &&
                task->result().data.value("project_id", nlohmann::json()) == preview_job) {
                const auto bytes = wxBase64Decode(wxString::FromUTF8(task->result().data.value("image_base64","")));
                wxMemoryInputStream stream(bytes.GetData(),bytes.GetDataLen());
                wxLogNull quiet;
                wxImage image(stream,wxBITMAP_TYPE_ANY);
                if (image.IsOk() && image.GetWidth()>0 && image.GetHeight()>0 && image.GetWidth()<=1024 && image.GetHeight()<=1024) {
                    const auto size=panel->FromDIP(64);
                    const double scale=double(size)/std::max(image.GetWidth(),image.GetHeight());
                    job_preview->SetBitmap(wxBitmap(image.Scale(std::max(1,int(image.GetWidth()*scale)),
                        std::max(1,int(image.GetHeight()*scale)),wxIMAGE_QUALITY_HIGH)));
                    job_preview->SetToolTip(_L("Current job preview"));
                }
            }
            refresh_state->preview.reset();
        }
        if (!refresh_state->preview && !refresh_state->preview_loaded && *saved && !preview_printer.is_null() && !preview_job.is_null()) {
            refresh_state->preview_loaded = true;
            refresh_state->preview = AnycubicCloudReadTask::start([preview_printer,preview_job](AnycubicCloudService::CancelFn cancelled) {
                AnycubicCloudService reader;
                return reader.request("job_preview",{{"printer_id",preview_printer},{"project_id",preview_job}}, {},std::move(cancelled));
            });
        }
        const auto monitor = refresh_state->monitor;
        if (monitor) {
            const auto wanted = selected_printer().value("id", nlohmann::json());
            if (!*saved || !refresh_state->monitor_enabled || wanted != refresh_state->monitor_printer)
                monitor->cancel();
            nlohmann::json frame;
            if (!monitor->cancelled() && monitor->take_update(frame) &&
                frame.value("printer_id", nlohmann::json()) == wanted) {
                refresh_state->live = std::move(frame);
                refresh_state->last_live_update = std::chrono::steady_clock::now();
                update_details();
            }
            if (monitor->ready()) {
                const bool retry_now = monitor->cancelled() || monitor->result().success;
                refresh_state->live = nlohmann::json::object();
                if (!retry_now) refresh_state->live = {{"error", monitor->result().error}};
                refresh_state->monitor.reset();
                refresh_state->monitor_next = retry_now ? std::chrono::steady_clock::time_point{} :
                    std::chrono::steady_clock::now() + std::chrono::seconds(30);
                update_details();
            }
        }
        const auto light_printer = selected_printer().value("id", nlohmann::json());
        if (refresh_state->light_status_printer != light_printer) {
            if (refresh_state->light_status) refresh_state->light_status->cancel();
            refresh_state->light_status_next = std::chrono::steady_clock::time_point{};
            refresh_state->light_status_printer = light_printer;
            refresh_state->light_command = nlohmann::json::object();
        }
        if (refresh_state->light_status && refresh_state->light_status->ready())
            refresh_state->light_status.reset();
        if (!*temperature_busy && !refresh_state->light_status && !light_printer.is_null() &&
            *saved && refresh_state->monitor_enabled && refresh_state->monitor &&
            !refresh_state->monitor->cancelled() && !refresh_state->monitor->ready() &&
            refresh_state->monitor_printer == light_printer && refresh_state->live.value("connected", false) &&
            refresh_state->live.value("light_reports_subscribed", false) &&
            std::chrono::steady_clock::now() >= refresh_state->light_status_next) {
            refresh_state->light_status_next = std::chrono::steady_clock::now() + std::chrono::seconds(45);
            // Read-only order 1232; never a light switch or camera command.
            refresh_state->light_status = AnycubicCloudReadTask::start([light_printer](AnycubicCloudService::CancelFn cancelled) {
                AnycubicCloudService reader;
                return reader.request("peripheral_control", {{"printer_id", light_printer},
                    {"action", "query_light"}, {"confirmed", true}}, {}, std::move(cancelled));
            });
        }
        const auto task = refresh_state->active;
        if (task && task->ready()) {
            const bool discard = task->cancelled() || !*saved || !panel->IsShownOnScreen();
            if (!discard) apply_refresh(task->result());
            refresh_state->active.reset();
            refresh_state->next = discard ? std::chrono::steady_clock::time_point{} :
                std::chrono::steady_clock::now() + std::chrono::seconds(task->result().success ? 30 : 60);
            refresh->Enable(*saved);
        }
        if (std::chrono::steady_clock::now() >= refresh_state->next) start_refresh();
        start_monitor();
    }, refresh_timer->GetId());
    panel->Bind(wxEVT_SHOW, [=](wxShowEvent& event) {
        if (event.GetEventObject() == panel) {
            if (event.IsShown()) {
                update_account();
                refresh_state->next = std::chrono::steady_clock::time_point{};
                refresh_timer->Start(200);
                start_refresh();
            } else {
                refresh_timer->Stop();
                if (refresh_state->preview) refresh_state->preview->cancel();
                if (refresh_state->light_status) refresh_state->light_status->cancel();
                if (camera) camera->stop();
                if (refresh_state->active) refresh_state->active->cancel();
                if (refresh_state->monitor) refresh_state->monitor->cancel();
                refresh_state->live = nlohmann::json::object();
            }
        }
        event.Skip();
    });
    panel->Bind(wxEVT_DESTROY, [=](wxWindowDestroyEvent& event) {
        if (event.GetEventObject() == panel) {
            refresh_state->closed = true;
            if (refresh_state->preview) refresh_state->preview->cancel();
            if (refresh_state->light_status) refresh_state->light_status->cancel();
            refresh_timer->Stop();
            if (refresh_state->active) refresh_state->active->cancel();
            if (refresh_state->monitor) refresh_state->monitor->cancel();
            refresh_state->live = nlohmann::json::object();
        }
        event.Skip();
    });
    refresh->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        start_refresh();
    });
    sync->Bind(wxEVT_UPDATE_UI, [=](wxUpdateUIEvent& event) {
        const auto printer = selected_printer();
        const auto model = wxString::FromUTF8(printer.value("model", ""));
        event.Enable(*saved && can_use_anycubic_cloud_filament_sync() && printer.contains("id") &&
                     (model.CmpNoCase("Kobra X") == 0 || model.CmpNoCase("Anycubic Kobra X") == 0));
    });
    sync->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        const auto printer = selected_printer();
        if (printer.contains("id")) sync_cloud_filaments(panel, *service, printer["id"]);
    });
    plate->Bind(wxEVT_UPDATE_UI, [=](wxUpdateUIEvent& event) {
        auto* plater = wxGetApp().plater();
        auto* current = plater ? plater->get_partplate_list().get_curr_plate() : nullptr;
        event.Enable(*saved && can_use_anycubic_cloud_filament_sync() && plater &&
                     !plater->is_background_process_slicing() && current && current->is_slice_result_ready_for_export());
    });
    plate->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        auto* plater = wxGetApp().plater();
        auto* current = plater ? plater->get_partplate_list().get_curr_plate() : nullptr;
        if (current && !plater->is_background_process_slicing() && current->is_slice_result_ready_for_export())
            upload_anycubic_cloud_plate(panel, current->get_gcode_filename());
    });
    import_button->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        wxFileDialog picker(panel, _L("Select encrypted Anycubic sign-in"), wxStandardPaths::Get().GetUserConfigDir() +
            "/OrcaCubic/orca_plugins/anycubic_cloud_orcacubic_test.py", "state.json",
            _L("JSON files (*.json)|*.json"), wxFD_OPEN | wxFD_FILE_MUST_EXIST);
        if (picker.ShowModal() != wxID_OK) return;
        if (camera) camera->stop();
        if (refresh_state->active) refresh_state->active->cancel();
        if (refresh_state->monitor) refresh_state->monitor->cancel();
        refresh_state->live = nlohmann::json::object();
        const auto result = run_request(panel, *service, "import_session", {{"path", std::string(picker.GetPath().ToUTF8())}});
        if (!result.success && result.error != "cancelled")
            wxMessageBox(_L("The sign-in could not be imported. Choose an encrypted sign-in file from your existing integration."),
                         _L("Anycubic Cloud"), wxOK | wxICON_ERROR, panel);
        clear_snapshot();
        update_account();
        refresh_state->next = std::chrono::steady_clock::time_point{};
    });
    logout->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        if (camera) camera->stop();
        if (refresh_state->active) refresh_state->active->cancel();
        if (refresh_state->monitor) refresh_state->monitor->cancel();
        refresh_state->live = nlohmann::json::object();
        const auto result = service->request("logout");
        if (!result.success)
            wxMessageBox(_L("The saved sign-in could not be removed."), _L("Anycubic Cloud"), wxOK | wxICON_ERROR, panel);
        clear_snapshot();
        update_account();
    });
    upload->Bind(wxEVT_BUTTON, [=](wxCommandEvent&) {
        wxFileDialog picker(panel, _L("Choose G-code for Anycubic Cloud"), wxStandardPaths::Get().GetDocumentsDir(), "",
                            _L("G-code files (*.gcode)|*.gcode"), wxFD_OPEN | wxFD_FILE_MUST_EXIST);
        if (picker.ShowModal() != wxID_OK) return;
        const auto result = run_request(panel, *service, "upload_gcode", {{"path", std::string(picker.GetPath().ToUTF8())}});
        if (result.success)
            wxMessageBox(result.cleanup_pending ? _L("The file was uploaded, but temporary storage cleanup could not be confirmed.") :
                _L("G-code uploaded to your Anycubic cloud library."), _L("Anycubic Cloud"),
                wxOK | (result.cleanup_pending ? wxICON_WARNING : wxICON_INFORMATION), panel);
        else if (result.error != "cancelled")
            wxMessageBox(print_failure_message(result), _L("Anycubic Cloud"), wxOK | wxICON_ERROR, panel);
    });
    update_account();
    update_details();
    return panel;
}


void show_anycubic_cloud_account(wxWindow* parent)
{
    wxDialog dialog(parent, wxID_ANY, _L("Anycubic Workbench"), wxDefaultPosition, wxDefaultSize,
                    wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER);
    auto* layout = new wxBoxSizer(wxVERTICAL);
    layout->Add(create_anycubic_cloud_workbench(&dialog), 1, wxEXPAND);
    layout->Add(dialog.CreateButtonSizer(wxOK), 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    dialog.SetSizerAndFit(layout);
    dialog.SetSize(dialog.FromDIP(wxSize(980, 800)));
    wxGetApp().UpdateDlgDarkUI(&dialog);
    dialog.CentreOnParent();
    dialog.ShowModal();
}

static void upload_cloud_library_file(wxWindow* parent, const std::string& path)
{
    if (wxMessageBox(_L("Upload the selected sliced plate to your Anycubic cloud library? This does not start a print."),
                     _L("Anycubic Cloud"), wxYES_NO | wxNO_DEFAULT | wxICON_QUESTION, parent) != wxYES)
        return;
    AnycubicCloudService service;
    const auto result = run_request(parent, service, "upload_gcode", {{"path", path}});
    wxString message;
    if (result.success)
        message = result.cleanup_pending ?
            _L("The file was uploaded, but temporary storage cleanup could not be confirmed.") :
            _L("G-code uploaded to your Anycubic cloud library.");
    else if (result.cleanup_pending)
        message = _L("The upload could not be confirmed. Check your cloud library before retrying.");
    else if (result.error == "cancelled")
        return;
    else if (result.error == "sign_in_required")
        message = _L("Open Anycubic Workbench and import your saved sign-in before uploading.");
    else if (result.error == "gcode_required" || result.error == "input_unreadable" || result.error == "input_empty")
        message = _L("The sliced G-code is unavailable. Slice the current plate again before uploading.");
    else
        message = _L("The G-code upload failed. Check your sign-in and internet connection.");
    wxMessageBox(message, _L("Anycubic Cloud"), wxOK | (result.success && !result.cleanup_pending ? wxICON_INFORMATION : wxICON_WARNING), parent);
}


static wxString print_failure_message(const AnycubicCloudResult& result)
{
    if (result.error == "api_rejected" && result.data.contains("api_code"))
        return wxString::Format(_L("Anycubic Cloud rejected the request (code %s). Check the printer and cloud library before trying again."),
            wxString::FromUTF8(result.data["api_code"].dump()));
    const std::map<std::string, wxString> messages = {
        {"sign_in_required", _L("Open Anycubic Workbench and import your saved sign-in before uploading.")},
        {"unsupported_printer", _L("Cloud printing currently supports Kobra X only. The cloud must identify its model.")},
        {"printer_missing", _L("The selected printer is no longer available on this account.")},
        {"printer_offline", _L("The selected printer is offline on Anycubic Cloud.")},
        {"printer_busy_or_unknown", _L("The printer is busy or its idle state could not be confirmed.")},
        {"unsupported_feeder", _L("Cloud printing currently supports the built-in Kobra X feeder only.")},
        {"feeder_unknown", _L("Feeder data is unavailable. Refresh the printer list before continuing.")},
        {"slot_unavailable", _L("A selected slot is empty or its filament presence is unknown.")},
        {"material_mismatch", _L("A selected slot does not match the sliced filament type.")},
        {"slot_color_unknown", _L("A selected filament color is unavailable or invalid.")},
        {"mapping_required", _L("Select a different matching slot for each used filament.")},
        {"mapping_changed", _L("The feeder data changed after selection. Refresh and review the mapping again.")},
        {"unsupported_materials", _L("This print uses unsupported or mixed filament indices. Library upload remains available.")},
        {"invalid_material", _L("The sliced filament data is incomplete. Slice the plate again.")},
        {"input_changed", _L("The sliced file changed during preparation. Slice the plate again.")},
        {"print_ticket_expired", _L("The prepared print expired. Upload and review it again.")},
        {"print_account_changed", _L("The sign-in changed after preparation. Upload and review the print again.")},
        {"print_in_progress", _L("Another operation is already preparing or starting a print on this printer.")},
        {"print_locked", _L("A previous print command is still protected against duplication. Check the printer before clearing its print protection.")},
        {"print_already_attempted", _L("This prepared print has already been submitted. It will not be sent again.")},
        {"start_outcome_unknown", _L("The print command outcome is unknown. Do not resend it. Check the printer in the Anycubic app; automatic retry is disabled.")},
        {"api_rejected", _L("Anycubic Cloud rejected the request. Check the printer and cloud library before trying again.")},
        {"print_state_unreadable", _L("Saved print protection could not be read. No new print command was sent.")}
    };
    const auto found = messages.find(result.error);
    if (found != messages.end()) return found->second;
    if (result.cleanup_pending)
        return _L("The upload could not be confirmed. Check your cloud library before retrying.");
    return _L("The cloud operation failed. Check the printer and your sign-in before trying again.");
}

static bool sync_cloud_filaments(wxWindow* parent, AnycubicCloudService& service, const nlohmann::json& printer_id)
{
    auto* bundle = wxGetApp().preset_bundle;
    auto* plater = wxGetApp().plater();
    if (!bundle || !plater || plater->is_background_process_slicing()) {
        wxMessageBox(_L("Wait for slicing to finish before syncing filaments."), _L("Anycubic Cloud"), wxOK | wxICON_INFORMATION, parent);
        return false;
    }
    const auto* model = bundle->printers.get_edited_preset().config.option<ConfigOptionString>("printer_model");
    if (!model || model->value != "Anycubic Kobra X") {
        wxMessageBox(_L("Select the Kobra X printer profile before syncing cloud filaments."), _L("Anycubic Cloud"), wxOK | wxICON_WARNING, parent);
        return false;
    }
    if (bundle->filament_presets.size() > 4 || bundle->num_mixed_filaments() != 0) {
        wxMessageBox(_L("Cloud sync does not replace projects with more than four filaments or mixed slots."), _L("Anycubic Cloud"), wxOK | wxICON_WARNING, parent);
        return false;
    }
    if (bundle->filaments.current_is_dirty()) {
        wxMessageBox(_L("Save or discard your unsaved filament profile edits before syncing. No profiles have been changed."), _L("Anycubic Cloud"), wxOK | wxICON_INFORMATION, parent);
        return false;
    }
    const auto refreshed = run_request(parent, service, "list_printers");
    if (!refreshed.success) {
        if (refreshed.error != "cancelled")
            wxMessageBox(print_failure_message(refreshed), _L("Anycubic Cloud"), wxOK | wxICON_WARNING, parent);
        return false;
    }
    nlohmann::json slots = nlohmann::json::array();
    for (const auto& printer : refreshed.data["printers"]) {
        if (!printer.contains("id") || printer["id"] != printer_id) continue;
        const auto hardware = wxString::FromUTF8(printer.value("model", ""));
        if (hardware.CmpNoCase("Kobra X") != 0 && hardware.CmpNoCase("Anycubic Kobra X") != 0) continue;
        if (printer.contains("boxes"))
            for (const auto& box : printer["boxes"])
                if (box["id"] == -1) slots = box["slots"];
    }
    bool complete = slots.size() == 4;
    for (int index = 0; index < 4 && complete; ++index)
        complete = slots[index]["index"] == index && slots[index]["present"] == true &&
                   !slots[index].value("type", "").empty() && !slots[index].value("color", "").empty();
    if (!complete) {
        wxMessageBox(_L("Full cloud sync requires confirmed type and color data for all four built-in slots. Unknown or empty slots are not guessed."),
                     _L("Anycubic Cloud"), wxOK | wxICON_WARNING, parent);
        return false;
    }
    std::vector<std::string> names;
    for (int index = 0; index < 4; ++index) {
        const auto& slot = slots[index];
        const auto type = wxString::FromUTF8(slot["type"].get<std::string>());
        const std::string cloud_name = slot.value("name", "");
        std::vector<AnycubicFilamentProfile> profiles;
        for (const auto& preset : bundle->filaments.get_presets()) {
            const auto* material = preset.config.option<ConfigOptionStrings>("filament_type");
            if (!preset.is_visible || !preset.is_compatible || !material || material->values.empty() ||
                type.CmpNoCase(wxString::FromUTF8(material->values.front())) != 0) continue;
            const auto* vendor = preset.config.option<ConfigOptionStrings>("filament_vendor");
            profiles.push_back({preset.name, preset.alias, vendor && !vendor->values.empty() ? vendor->values.front() : ""});
        }
        if (profiles.empty()) {
            wxMessageBox(wxString::Format(_L("No enabled compatible local profile exists for %s. Enable a matching filament profile, then sync again."), type),
                         _L("Anycubic Cloud"), wxOK | wxICON_INFORMATION, parent);
            return false;
        }
        const std::string current = std::size_t(index) < bundle->filament_presets.size() ? bundle->filament_presets[index] : "";
        const int preferred = match_anycubic_filament_profile(profiles, cloud_name, slot["type"].get<std::string>(), current);
        if (preferred < 0 || std::size_t(preferred) >= profiles.size()) return false;
        names.push_back(profiles[preferred].name);
    }
    // The cloud request can process UI events; validate the current project again.
    bool valid = !plater->is_background_process_slicing() && !bundle->filaments.current_is_dirty() &&
                 bundle->filament_presets.size() <= 4 && bundle->num_mixed_filaments() == 0;
    const auto* current_model = bundle->printers.get_edited_preset().config.option<ConfigOptionString>("printer_model");
    valid = valid && current_model && current_model->value == "Anycubic Kobra X";
    for (std::size_t index = 0; index < names.size() && valid; ++index) {
        const auto* preset = bundle->filaments.find_preset(names[index]);
        const auto* type = preset ? preset->config.option<ConfigOptionStrings>("filament_type") : nullptr;
        valid = preset && preset->is_visible && preset->is_compatible && type && !type->values.empty() &&
            wxString::FromUTF8(type->values.front()).CmpNoCase(wxString::FromUTF8(slots[index]["type"].get<std::string>())) == 0;
    }
    if (!valid) {
        wxMessageBox(_L("The project or available profiles changed. Refresh and sync filaments again."),
                     _L("Anycubic Cloud"), wxOK | wxICON_WARNING, parent);
        return false;
    }
    plater->take_snapshot(std::string(_L("Sync Anycubic cloud filaments").ToUTF8()));
    plater->sidebar().finish_param_edit();
    bundle->set_num_filaments(4);
    for (std::size_t index = 0; index < 4; ++index)
        bundle->set_filament_preset(index, names[index]);
    bundle->filaments.select_preset_by_name(names.front(), true);
    auto* colors = bundle->project_config.option<ConfigOptionStrings>("filament_colour");
    auto* multi = bundle->project_config.option<ConfigOptionStrings>("filament_multi_colour");
    auto* color_types = bundle->project_config.option<ConfigOptionStrings>("filament_colour_type");
    for (std::size_t index = 0; index < 4; ++index) {
        colors->values[index] = slots[index]["color"].get<std::string>();
        if (multi) multi->values[index] = colors->values[index];
        if (color_types) color_types->values[index] = "1";
    }
    plater->on_filament_count_change(4);
    if (auto* tab = wxGetApp().get_tab(Preset::TYPE_FILAMENT)) tab->load_current_preset();
    plater->sidebar().update_presets(Preset::TYPE_FILAMENT);
    plater->on_config_change(bundle->full_config());
    auto& plates = plater->get_partplate_list();
    for (int index = 0; index < plates.get_plate_count(); ++index)
        plates.get_plate(index)->update_slice_result_valid_state(false);
    if (wxGetApp().app_config->get("auto_calculate_flush") != "disabled")
        plater->sidebar().auto_calc_flushing_volumes();
    bundle->export_selections(*wxGetApp().app_config);
    plater->update();
    return true;
}

void show_anycubic_cloud_filament_sync(wxWindow* parent)
{
    if (!can_use_anycubic_cloud_filament_sync()) return;
    AnycubicCloudService service;
    const auto result = run_request(parent, service, "list_printers");
    if (!result.success) {
        if (result.error != "cancelled")
            wxMessageBox(print_failure_message(result), _L("Anycubic Cloud"), wxOK | wxICON_WARNING, parent);
        return;
    }
    nlohmann::json printers = nlohmann::json::array();
    wxArrayString names;
    for (const auto& printer : result.data["printers"]) {
        const auto model = wxString::FromUTF8(printer.value("model", ""));
        if (!printer.contains("id") || (model.CmpNoCase("Kobra X") != 0 && model.CmpNoCase("Anycubic Kobra X") != 0))
            continue;
        printers.push_back(printer);
        names.Add(wxString::FromUTF8(printer.value("name", "Kobra X")));
    }
    if (printers.empty()) {
        wxMessageBox(_L("No Kobra X is available on this cloud account. Check your sign-in and printer list."),
                     _L("Anycubic Cloud"), wxOK | wxICON_INFORMATION, parent);
        return;
    }
    int selected = 0;
    if (printers.size() > 1) {
        wxSingleChoiceDialog picker(parent, _L("Select the printer to sync filaments from."),
                                     _L("Sync Anycubic cloud filaments"), names);
        wxGetApp().UpdateDlgDarkUI(&picker);
        if (picker.ShowModal() != wxID_OK) return;
        selected = picker.GetSelection();
        if (selected == wxNOT_FOUND) return;
    }
    sync_cloud_filaments(parent, service, printers[selected]["id"]);
}

static nlohmann::json current_plate_materials(const std::string& path)
{
    auto* plater = wxGetApp().plater();
    auto* bundle = wxGetApp().preset_bundle;
    if (!plater || !bundle || plater->is_background_process_slicing()) return nlohmann::json::array();
    auto* plate = plater->get_partplate_list().get_curr_plate();
    if (!plate || !plate->is_slice_result_ready_for_export() || plate->get_gcode_filename() != path ||
        !plate->get_slice_result() || plate->get_objects_on_this_plate().empty())
        return nlohmann::json::array();
    const auto config = bundle->full_config();
    const auto* model = config.option<ConfigOptionString>("printer_model");
    const auto* types = config.option<ConfigOptionStrings>("filament_type");
    const auto* colors = config.option<ConfigOptionStrings>("filament_colour");
    if (!model || model->value != "Anycubic Kobra X" || !types || !colors) return nlohmann::json::array();
    const auto* sliced = plate->get_slice_result();
    nlohmann::json materials = nlohmann::json::array();
    for (const int extruder : plate->get_extruders(true, false)) {
        if (extruder < 1 || extruder > 4) return nlohmann::json::array();
        const auto index = static_cast<std::size_t>(extruder - 1);
        if (index >= types->values.size() || index >= colors->values.size()) return nlohmann::json::array();
        double weight = 0;
        const auto volume = sliced->print_statistics.total_volumes_per_extruder.find(index);
        if (volume != sliced->print_statistics.total_volumes_per_extruder.end()) {
            if (index >= sliced->filament_densities.size()) return nlohmann::json::array();
            weight = volume->second * sliced->filament_densities[index] * 0.001;
        }
        if (!std::isfinite(weight) || weight < 0) return nlohmann::json::array();
        materials.push_back({{"paint_index", index}, {"material_type", types->values[index]},
                             {"paint_color", colors->values[index]}, {"filament_used", weight}});
    }
    return materials;
}

void upload_anycubic_cloud_plate(wxWindow* parent, const std::string& path)
{
    const auto materials = current_plate_materials(path);
    wxDialog dialog(parent, wxID_ANY, _L("Anycubic Cloud - current plate (Dev16)"), wxDefaultPosition, wxDefaultSize,
                    wxDEFAULT_DIALOG_STYLE | wxRESIZE_BORDER);
    auto* layout = new wxBoxSizer(wxVERTICAL);
    auto* hint = new wxStaticText(&dialog, wxID_ANY,
        _L("Library upload does not start a print. To print, refresh printers, select Kobra X and map every used filament to its built-in feeder slot."));
    hint->Wrap(dialog.FromDIP(660));
    layout->Add(hint, 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    auto* printer_choice = new wxChoice(&dialog, wxID_ANY);
    layout->Add(printer_choice, 0, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, dialog.FromDIP(12));
    auto* refresh = new wxButton(&dialog, wxID_ANY, _L("Refresh printers"));
    auto* sync = new wxButton(&dialog, wxID_ANY, _L("Sync filaments to project..."));
    layout->Add(refresh, 0, wxLEFT | wxRIGHT | wxBOTTOM, dialog.FromDIP(12));
    layout->Add(sync, 0, wxLEFT | wxRIGHT | wxBOTTOM, dialog.FromDIP(12));
    std::vector<BitmapComboBox*> choices;
    std::vector<std::vector<int>> slot_indices;
    std::vector<std::vector<bool>> slot_compatible;
    for (const auto& material : materials) {
        auto* label = new wxStaticText(&dialog, wxID_ANY, wxString::Format(_L("Filament %s: %s (%s)"),
            wxString::FromUTF8(std::to_string(material["paint_index"].get<int>() + 1)),
            wxString::FromUTF8(material["material_type"].get<std::string>()),
            wxString::FromUTF8(material["paint_color"].get<std::string>())));
        layout->Add(label, 0, wxLEFT | wxRIGHT | wxBOTTOM, dialog.FromDIP(8));
        auto* choice = new BitmapComboBox(&dialog, wxID_ANY, wxEmptyString, wxDefaultPosition, wxDefaultSize,
                                           0, nullptr, wxCB_READONLY);
        layout->Add(choice, 0, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, dialog.FromDIP(12));
        choices.push_back(choice);
        slot_indices.emplace_back();
        slot_compatible.emplace_back();
    }
    if (materials.empty()) {
        auto* unavailable = new wxStaticText(&dialog, wxID_ANY,
            _L("Cloud printing requires a freshly sliced Kobra X plate with up to four standard filaments. Library upload is still available."));
        unavailable->Wrap(dialog.FromDIP(660));
        layout->Add(unavailable, 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    }
    auto* slot_hint = new wxStaticText(&dialog, wxID_ANY,
        _L("All four slots are shown with their reported colors. Empty, unknown or different materials cannot be used for this print."));
    slot_hint->Wrap(dialog.FromDIP(660));
    layout->Add(slot_hint, 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    auto* calibration_box = new wxStaticBoxSizer(wxHORIZONTAL, &dialog, _L("Calibration"));
    auto* auto_leveling = new wxCheckBox(&dialog, wxID_ANY, _L("Auto Leveling"));
    auto* vibration_compensation = new wxCheckBox(&dialog, wxID_ANY, _L("Resonance Compensation"));
    auto* flow_calibration = new wxCheckBox(&dialog, wxID_ANY, _L("Flow Calibration"));
    auto_leveling->SetValue(true);
    auto_leveling->SetToolTip(_L("Probe and compensate the build plate before this print."));
    vibration_compensation->SetToolTip(_L("Run vibration compensation before this print."));
    flow_calibration->SetToolTip(_L("Calibrate extrusion flow before this print."));
    for (auto* option : {auto_leveling, vibration_compensation, flow_calibration})
        calibration_box->Add(option, 0, wxALL, dialog.FromDIP(4));
    layout->Add(calibration_box, 0, wxEXPAND | wxLEFT | wxRIGHT | wxBOTTOM, dialog.FromDIP(12));
    auto* buttons = new wxBoxSizer(wxHORIZONTAL);
    auto* library = new wxButton(&dialog, wxID_ANY, _L("Upload to cloud library"));
    auto* prepare = new wxButton(&dialog, wxID_ANY, _L("Upload and review print..."));
    auto* clear = new wxButton(&dialog, wxID_ANY, _L("Clear previous print protection..."));
    buttons->Add(library, 0, wxRIGHT, dialog.FromDIP(8));
    buttons->Add(prepare, 0);
    layout->Add(buttons, 0, wxALL, dialog.FromDIP(12));
    layout->Add(clear, 0, wxLEFT | wxRIGHT | wxBOTTOM, dialog.FromDIP(12));
    layout->Add(dialog.CreateButtonSizer(wxCANCEL), 0, wxEXPAND | wxALL, dialog.FromDIP(12));
    dialog.SetSizerAndFit(layout);
    wxGetApp().UpdateDlgDarkUI(&dialog);
    dialog.CentreOnParent();
    AnycubicCloudService service;
    nlohmann::json printers = nlohmann::json::array();
    auto update_enabled = [&]() {
        bool ready = !materials.empty() && printer_choice->GetSelection() != wxNOT_FOUND;
        for (std::size_t row = 0; row < choices.size(); ++row) {
            const int item = choices[row]->GetSelection();
            ready = ready && item != wxNOT_FOUND && std::size_t(item) < slot_compatible[row].size() && slot_compatible[row][item];
        }
        prepare->Enable(ready);
        clear->Enable(printer_choice->GetSelection() != wxNOT_FOUND);
        sync->Enable(printer_choice->GetSelection() != wxNOT_FOUND);
    };
    auto update_slots = [&]() {
        for (std::size_t row = 0; row < choices.size(); ++row) {
            choices[row]->Clear();
            slot_indices[row].clear();
            slot_compatible[row].clear();
            const int selected = printer_choice->GetSelection();
            if (selected == wxNOT_FOUND) continue;
            const auto& printer = printers[selected];
            for (int index = 0; index < 4; ++index) {
                nlohmann::json slot = {{"present", nullptr}, {"type", ""}, {"color", ""}};
                if (printer.contains("boxes")) {
                    for (const auto& box : printer["boxes"]) {
                        if (box["id"] != -1) continue;
                        for (const auto& reported : box["slots"])
                            if (reported["index"] == index) slot = reported;
                    }
                }
                const wxString type = wxString::FromUTF8(slot.value("type", ""));
                const auto color = slot.value("color", "");
                const bool matches = type.CmpNoCase(wxString::FromUTF8(materials[row]["material_type"].get<std::string>())) == 0;
                const bool compatible = slot["present"] == true && !color.empty() && matches;
                const wxString state = slot["present"].is_null() ? _L("Unknown") :
                    slot["present"] == false ? _L("Empty") : !matches ? _L("Different material") :
                    color.empty() ? _L("Color unavailable") : _L("Compatible");
                const wxString shown_type = type.empty() ? _L("Unknown") : type;
                slot_indices[row].push_back(index);
                slot_compatible[row].push_back(compatible);
                choices[row]->Append(wxString::Format(_L("Slot %s: %s - %s"),
                    wxString::FromUTF8(std::to_string(index + 1)), shown_type, state),
                    filament_swatch(choices[row], color));
            }
        }
        update_enabled();
    };
    for (auto* choice : choices)
        choice->Bind(wxEVT_COMBOBOX, [&](wxCommandEvent&) { update_enabled(); });
    printer_choice->Bind(wxEVT_CHOICE, [&](wxCommandEvent&) { update_slots(); });
    refresh->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) {
        const auto result = run_request(&dialog, service, "list_printers");
        if (!result.success) {
            if (result.error != "cancelled")
                wxMessageBox(print_failure_message(result), _L("Anycubic Cloud"), wxOK | wxICON_WARNING, &dialog);
            return;
        }
        printers = result.data.value("printers", nlohmann::json::array());
        printer_choice->Clear();
        for (const auto& printer : printers)
            printer_choice->Append(wxString::Format(_L("%s (%s)"),
                wxString::FromUTF8(printer.value("name", "Anycubic Printer")), cloud_printer_state(printer)));
        // Printer and slots always require an explicit selection for a print.
        update_slots();
    });
    sync->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) {
        const int selected = printer_choice->GetSelection();
        if (selected != wxNOT_FOUND && sync_cloud_filaments(&dialog, service, printers[selected]["id"]))
            dialog.EndModal(wxID_CANCEL);
    });
    library->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) {
        upload_cloud_library_file(&dialog, path);
        dialog.EndModal(wxID_OK);
    });
    clear->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) {
        const int selected = printer_choice->GetSelection();
        if (selected == wxNOT_FOUND) return;
        if (wxMessageBox(_L("Clear previous print protection only after checking the actual printer and the Anycubic app. Confirm that the previous job is finished or was not started and that no print is queued. Clear protection?"),
                         _L("Anycubic Cloud"), wxYES_NO | wxNO_DEFAULT | wxICON_WARNING, &dialog) != wxYES) return;
        const auto result = run_request(&dialog, service, "clear_print_lock",
            {{"printer_id", printers[selected]["id"]}, {"confirmed", true}});
        wxMessageBox(result.success ? _L("Previous print protection cleared. You can prepare a new print.") : print_failure_message(result),
                     _L("Anycubic Cloud"), wxOK | (result.success ? wxICON_INFORMATION : wxICON_WARNING), &dialog);
    });
    prepare->Bind(wxEVT_BUTTON, [&](wxCommandEvent&) {
        const int selected = printer_choice->GetSelection();
        if (selected == wxNOT_FOUND || current_plate_materials(path) != materials) {
            wxMessageBox(_L("The sliced filament data is incomplete. Slice the plate again."),
                         _L("Anycubic Cloud"), wxOK | wxICON_WARNING, &dialog);
            return;
        }
        nlohmann::json slots = nlohmann::json::array();
        for (std::size_t row = 0; row < choices.size(); ++row) {
            const int item = choices[row]->GetSelection();
            if (item == wxNOT_FOUND || !slot_compatible[row][item]) return;
            slots.push_back({{"paint_index", materials[row]["paint_index"]}, {"slot", slot_indices[row][item]}});
        }
        const nlohmann::json calibration = {{"auto_leveling", auto_leveling->GetValue()},
            {"vibration_compensation", vibration_compensation->GetValue()}, {"flow_calibration", flow_calibration->GetValue()}};
        const auto plan = run_request(&dialog, service, "prepare_print",
            {{"printer_id", printers[selected]["id"]}, {"materials", materials}, {"slots", slots},
             {"path", path}, {"calibration", calibration}});
        if (!plan.success) {
            if (plan.error != "cancelled")
                wxMessageBox(print_failure_message(plan), _L("Anycubic Cloud"), wxOK | wxICON_WARNING, &dialog);
            return;
        }
        wxString summary = wxString::Format(_L("Start this print on %s now?"),
            wxString::FromUTF8(plan.data.value("printer_name", "Kobra X"))) + "\n\n";
        for (const auto& mapping : plan.data["mapping"])
            summary += wxString::Format(_L("Filament %s -> built-in slot %s: %s (%s)"),
                wxString::FromUTF8(std::to_string(mapping["paint_index"].get<int>() + 1)),
                wxString::FromUTF8(std::to_string(mapping["ams_index"].get<int>() + 1)),
                wxString::FromUTF8(mapping["material_type"].get<std::string>()),
                wxString::Format("#%02X%02X%02X", mapping["ams_color"][0].get<int>(),
                                 mapping["ams_color"][1].get<int>(), mapping["ams_color"][2].get<int>())) + "\n";
        const auto reviewed_calibration = plan.data.value("calibration", nlohmann::json::object());
        summary += "\n" + wxString::Format(
            _L("Before printing: bed leveling %s, vibration compensation %s, flow calibration %s."),
            reviewed_calibration.value("auto_leveling", 0) ? _L("On") : _L("Off"),
            reviewed_calibration.value("vibration_compensation", 0) ? _L("On") : _L("Off"),
            reviewed_calibration.value("flow_calibration", 0) ? _L("On") : _L("Off")) + "\n";
        if (plan.cleanup_pending)
            summary += "\n" + _L("The file was uploaded, but temporary storage cleanup could not be confirmed.") + "\n";
        summary += "\n" + _L("Confirm the bed is clear, the nozzle matches your profile and the selected slots contain the correct filaments. This command starts the physical printer.");
        if (wxMessageBox(summary, _L("Confirm cloud print"), wxYES_NO | wxNO_DEFAULT | wxICON_WARNING, &dialog) != wxYES) {
            dialog.EndModal(wxID_CANCEL);
            return;
        }
        // Keep the slicer's toolpath/G-code/filament checks authoritative even
        // if a delayed validation result arrived while confirmation was open.
        if (current_plate_materials(path) != materials) {
            wxMessageBox(_L("The sliced filament data is incomplete. Slice the plate again."),
                         _L("Anycubic Cloud"), wxOK | wxICON_WARNING, &dialog);
            dialog.EndModal(wxID_CANCEL);
            return;
        }
        const auto started = run_request(&dialog, service, "start_print",
            {{"printer_id", plan.data["printer_id"]}, {"ticket", plan.data["ticket"]}, {"confirmed", true}});
        wxMessageBox(started.success ?
            _L("Anycubic Cloud accepted the print command. Check the printer in the Anycubic app; this acknowledgement does not confirm physical printing.") :
            print_failure_message(started), _L("Anycubic Cloud"), wxOK | (started.success ? wxICON_INFORMATION : wxICON_WARNING), &dialog);
        dialog.EndModal(wxID_OK);
    });
    update_enabled();
    dialog.ShowModal();
}

} // namespace Slic3r::GUI
