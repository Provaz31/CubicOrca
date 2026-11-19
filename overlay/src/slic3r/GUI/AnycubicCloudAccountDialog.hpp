#pragma once

#include <string>

class wxWindow;

namespace Slic3r::GUI {
bool can_use_anycubic_cloud_filament_sync();
void show_anycubic_cloud_filament_sync(wxWindow* parent);
wxWindow* create_anycubic_cloud_workbench(wxWindow* parent);
void show_anycubic_cloud_account(wxWindow* parent);
void show_anycubic_cloud_updates(wxWindow* parent);
void upload_anycubic_cloud_plate(wxWindow* parent, const std::string& path);
}
