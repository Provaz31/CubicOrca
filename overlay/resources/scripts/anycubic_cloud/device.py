"""Bounded device metadata for the native Workbench. No control commands."""

import json
import math
from datetime import datetime, timezone


def _text(value, limit=128):
    return ''.join(c for c in value if c.isprintable())[:limit] if isinstance(value, str) else ''


def _number(value, maximum):
    if isinstance(value, str) and len(value) <= 32:
        try:
            value = float(value)
        except ValueError:
            return None
    return value if type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value) else None


def _object(value):
    if isinstance(value, str) and len(value) <= 65536:
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _identifier(value):
    if type(value) in (int, str):
        text = str(value)
        if len(text) <= 19 and text.isascii() and text.isdigit() and 0 < int(text) < 2**63:
            return str(int(text))
    return None


def _light_type(value):
    """A reported protocol enum, not a printer/account ID. Zero is valid."""
    if type(value) not in (int, str):
        return None
    text = str(value)
    if not 1 <= len(text) <= 10 or not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if number < 2**31 else None


def _report_time(value):
    number = _number(value, 4102444800000)
    if number is not None:
        # Known Unix seconds/milliseconds range, 2000-2100. Zero means missing.
        if number >= 946684800000:
            number /= 1000
        return (datetime.fromtimestamp(number, timezone.utc).isoformat(timespec='seconds')
                .replace('+00:00', 'Z')) if 946684800 <= number <= 4102444800 else ''
    return _text(value, 64)


def public_telemetry(printer, projects):
    """Cloud snapshots only, never inferred live measurements or control state.

    Keep a strict field allowlist. Do not walk arbitrary nested records: those
    can contain unrelated historical temperatures, URLs or account secrets.
    """
    result = {}
    parameter = _object(printer.get('parameter'))
    for source, target, maximum in (
            ('curr_nozzle_temp', 'nozzle', 500), ('curr_hotbed_temp', 'bed', 200)):
        value = _number(parameter.get(source), maximum)
        if value is not None:
            result[target] = value
    # A successful HTTP read is not evidence of recent printer telemetry.
    result['reported_at'] = _report_time(printer.get('last_update_time'))
    wanted = _identifier(printer.get('id'))
    active = [item for item in projects if isinstance(item, dict) and wanted is not None and
              _identifier(item.get('printer_id')) == wanted and
              _number(item.get('print_status'), 10) in (1, 4, 5, 6, 7)]
    # Never attach an old active-looking task to an idle/offline printer. An
    # ambiguous task list is also unknown; list order is not an identity check.
    if (len(active) != 1 or _number(printer.get('is_printing'), 2) != 2 or
            _number(printer.get('device_status'), 1) != 1):
        return result
    project = active[0]
    settings = _object(project.get('settings'))
    job = {'name': _text(project.get('gcode_name'), 256)}
    project_id = _identifier(project.get('id'))
    if project_id is not None: job['project_id'] = int(project_id)
    for source, target, maximum, record in (
            ('progress', 'progress', 100, project), ('remain_time', 'remaining_minutes', 525600, project),
            ('curr_layer', 'layer', 1000000, settings), ('total_layers', 'layers', 1000000, settings)):
        value = _number(record.get(source), maximum)
        if value is not None and (target in ('progress', 'remaining_minutes') or value == int(value)):
            job[target] = value
    if 'layer' in job and 'layers' in job and job['layer'] > job['layers']:
        del job['layer']
        del job['layers']
    result['job'] = job
    return result


def public_printer(record):
    result = {key: _text(record[key]) for key in ('name', 'model', 'firmware_version')
              if isinstance(record.get(key), str)}
    for key in ('id', 'device_status', 'is_printing', 'machine_type'):
        value = record.get(key)
        if type(value) in (str, int, bool):
            result[key] = _text(value) if isinstance(value, str) else value
    if 'firmware_version' in result:
        result['firmware_version'] = result['firmware_version'].strip()
    if not result.get('firmware_version'):
        version = record.get('version')
        # Cloud printer records can carry a firmware-info object. Forward only
        # the installed version, never update URLs, notes or the target version.
        if isinstance(version, dict):
            version = version.get('firmware_version')
        if isinstance(version, str) and _text(version).strip():
            result['firmware_version'] = _text(version).strip()
    raw = record.get('multi_color_box')
    if not isinstance(raw, list):
        return result  # Missing data must not look like an empty feeder.
    boxes, seen = [], set()
    for item in raw[:16]:
        if not isinstance(item, dict):
            continue
        box_id = item.get('id')
        if type(box_id) is not int or not -1 <= box_id <= 3 or box_id in seen:
            continue
        seen.add(box_id)
        slots, indexes = [], set()
        raw_slots = item.get('slots')
        for slot in raw_slots[:16] if isinstance(raw_slots, list) else []:
            if not isinstance(slot, dict):
                continue
            index = slot.get('index')
            if type(index) is not int or not 0 <= index <= 3 or index in indexes:
                continue
            indexes.add(index)
            status = slot.get('status')
            edit_status = slot.get('edit_status')
            # Empty slots can retain their last material. Prefer the explicit
            # tag/manual/empty marker; status 5 alone confirms a loaded slot.
            present = (edit_status != 2 if type(edit_status) is int and edit_status in (0, 1, 2)
                       else True if type(status) is int and status == 5 else None)
            rgb = slot.get('color')
            color = ('#' + ''.join('%02X' % c for c in rgb[:3]) if isinstance(rgb, list) and
                     len(rgb) in (3, 4) and all(type(c) is int and 0 <= c <= 255 for c in rgb) else '')
            slots.append({'index': index, 'present': present, 'color': color if present else '',
                          'type': _text(slot.get('type'), 64) if present else '',
                          'name': _text(slot.get('material_name'), 256) if present else ''})
        boxes.append({'id': box_id, 'slots': sorted(slots, key=lambda value: value['index'])})
    result['boxes'] = sorted(boxes, key=lambda value: value['id'])
    return result
