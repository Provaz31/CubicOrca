#pragma once

#include <cstddef>
#include <initializer_list>
#include <string>
#include <vector>

namespace Slic3r::GUI {

struct AnycubicFilamentProfile {
    std::string name;
    std::string alias;
    std::string vendor;
};

inline std::string anycubic_filament_name_key(const std::string& name, bool strip_printer)
{
    std::string key;
    bool space = false;
    for (unsigned char ch : name) {
        if (strip_printer && ch == '@') break;
        if (ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n') {
            space = !key.empty();
            continue;
        }
        if (space) key += ' ';
        space = false;
        key += static_cast<char>(ch >= 'A' && ch <= 'Z' ? ch + ('a' - 'A') : ch);
    }
    return key;
}

// The caller supplies only enabled, printer-compatible profiles of the slot's
// material type. A material identity takes priority over the project's old slot.
inline int match_anycubic_filament_profile(const std::vector<AnycubicFilamentProfile>& profiles,
                                          const std::string& cloud_name, const std::string& material_type,
                                          const std::string& current)
{
    const auto full = anycubic_filament_name_key(cloud_name, false);
    const auto base = anycubic_filament_name_key(cloud_name, true);
    if (!base.empty() && base != anycubic_filament_name_key(material_type, true)) {
        for (bool strip_printer : {false, true}) {
            int match = -1;
            const auto& key = strip_printer ? base : full;
            for (std::size_t index = 0; index < profiles.size(); ++index) {
                const auto& profile = profiles[index];
                if (anycubic_filament_name_key(profile.name, strip_printer) == key ||
                    (strip_printer && !profile.alias.empty() &&
                     anycubic_filament_name_key(profile.alias, true) == key)) {
                    if (match != -1) { match = -1; break; }
                    match = static_cast<int>(index);
                }
            }
            if (match != -1) return match;
        }
    }
    // Missing or ambiguous identity uses a local default of the confirmed type.
    // Keep an existing compatible choice so later syncs preserve manual changes.
    for (std::size_t index = 0; index < profiles.size(); ++index)
        if (profiles[index].name == current) return static_cast<int>(index);
    const auto type = anycubic_filament_name_key(material_type, true);
    int preferred = -1;
    int best_rank = 5;
    std::string best_name;
    for (std::size_t index = 0; index < profiles.size(); ++index) {
        const auto& profile = profiles[index];
        const auto name = anycubic_filament_name_key(profile.name, true);
        const auto alias = anycubic_filament_name_key(profile.alias, true);
        const auto vendor = anycubic_filament_name_key(profile.vendor, false);
        const int rank = name == "anycubic " + type || alias == "anycubic " + type ? 0 :
                         name == "generic " + type || alias == "generic " + type ? 1 :
                         vendor == "anycubic" || name.rfind("anycubic ", 0) == 0 ? 2 :
                         vendor == "generic" || name.rfind("generic ", 0) == 0 ? 3 : 4;
        if (preferred == -1 || rank < best_rank || (rank == best_rank && profile.name < best_name)) {
            preferred = static_cast<int>(index);
            best_rank = rank;
            best_name = profile.name;
        }
    }
    return preferred;
}

} // namespace Slic3r::GUI
