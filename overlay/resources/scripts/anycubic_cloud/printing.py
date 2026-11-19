"""Confirmed cloud starts, with durable duplicate protection and no retries.

Cloud order fields follow the existing Anycubic protocol references. This
initial implementation supports the four built-in Kobra X slots only. Box
identity (-1) is kept separate from the command's zero-based slot index.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from .client import CloudError
from .device import public_printer
from . import protocol


def identifier(value):
    if type(value) not in (str, int) or not re.fullmatch(r'[0-9]{1,19}', str(value)) or not 0 < int(value) < 2**63:
        raise CloudError('invalid_request')
    return int(value)


def _one(value):
    return type(value) in (int, str) and str(value) == '1'


def _busy(value):
    return type(value) in (int, str) and str(value) == '2'


def _material(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9 +_.-]{1,64}', value):
        raise CloudError('invalid_material')
    normalized = value.strip().upper()
    if not normalized:
        raise CloudError('invalid_material')
    return normalized


def _rgb(value):
    if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?', value):
        return [int(value[i:i+2], 16) for i in (1, 3, 5)]
    if isinstance(value, list) and len(value) in (3, 4) and all(type(c) is int and 0 <= c <= 255 for c in value):
        return value[:3]
    raise CloudError('slot_color_unknown')


def validate_materials(materials):
    if not isinstance(materials, list) or not 1 <= len(materials) <= 4:
        raise CloudError('unsupported_materials')
    result, seen = [], set()
    for item in materials:
        if not isinstance(item, dict) or set(item) != {'paint_index', 'material_type', 'paint_color', 'filament_used'}:
            raise CloudError('invalid_material')
        index, amount = item['paint_index'], item['filament_used']
        if type(index) is not int or not 0 <= index <= 3 or index in seen:
            raise CloudError('unsupported_materials')
        if type(amount) not in (int, float) or not math.isfinite(amount) or not 0 <= amount <= 100000:
            raise CloudError('invalid_material')
        seen.add(index)
        result.append({'paint_index': index, 'material_type': _material(item['material_type']),
                       'paint_color': _rgb(item['paint_color']), 'filament_used': float(amount)})
    return sorted(result, key=lambda item: item['paint_index'])


def build_mapping(printer, materials, selections):
    # Missing/duplicated identities must not silently select a different feeder.
    boxes = printer.get('multi_color_box')
    if not isinstance(boxes, list):
        raise CloudError('feeder_unknown')
    built_in = [box for box in boxes if isinstance(box, dict) and type(box.get('id')) is int and box['id'] == -1]
    if len(built_in) != 1 or not isinstance(built_in[0].get('slots'), list):
        raise CloudError('unsupported_feeder')
    if not isinstance(selections, list) or len(selections) != len(materials):
        raise CloudError('mapping_required')
    selected = {}
    for item in selections:
        if not isinstance(item, dict) or set(item) != {'paint_index', 'slot'}:
            raise CloudError('mapping_required')
        index, slot = item['paint_index'], item['slot']
        if type(index) is not int or type(slot) is not int or not 0 <= slot <= 3 or index in selected:
            raise CloudError('mapping_required')
        selected[index] = slot
    if set(selected) != {item['paint_index'] for item in materials} or len(set(selected.values())) != len(selected):
        raise CloudError('mapping_required')
    normalized = public_printer(printer)['boxes']
    public_slots = next(box['slots'] for box in normalized if box['id'] == -1)
    result = []
    for material in materials:
        slot_id = selected[material['paint_index']]
        raw = [slot for slot in built_in[0]['slots'] if isinstance(slot, dict) and
               type(slot.get('index')) is int and slot['index'] == slot_id]
        current = next((slot for slot in public_slots if slot['index'] == slot_id), None)
        if len(raw) != 1 or current is None or current['present'] is not True:
            raise CloudError('slot_unavailable')
        if _material(current['type']) != material['material_type']:
            raise CloudError('material_mismatch')
        result.append({**material, 'ams_index': slot_id, 'ams_color': _rgb(raw[0].get('color'))})
    return result


def validate_calibration(value):
    # Missing options preserve the payload of older clients and prepared tickets.
    if value is None:
        return {}
    keys = {'auto_leveling', 'vibration_compensation', 'flow_calibration'}
    if not isinstance(value, dict) or set(value) != keys or any(type(v) is not bool for v in value.values()):
        raise CloudError('invalid_request')
    return {key: int(value[key]) for key in sorted(keys)}


def print_body(file_id, printer_id, mapping, calibration=None):
    return {'order_id': 1, 'printer_id': printer_id, 'project_id': 0,
            'data': {'filetype': 0, 'file_key': '', 'file_name': '', 'file_id': file_id,
                     'task_settings': {'ai_detect': 0, 'camera_timelapse': 0, **(calibration or {})},
                     'hollow_param': None, 'is_delete_file': 0, 'matrix': '', 'project_type': 1,
                     'punching_param': None, 'slice_param': None, 'slice_size': None, 'template_id': 0},
            'ams_info': {'use_ams': True, 'ams_box_mapping': mapping}, 'settings': None}


class PrintJobs:
    def __init__(self, root):
        self.root = Path(root)

    def _folder(self, printer_id):
        return self.root / str(identifier(printer_id))

    @contextmanager
    def lock(self, printer_id):
        folder = self._folder(printer_id)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / 'operation.lock').open('a+b') as stream:
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise CloudError('print_in_progress') from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def load(self, printer_id):
        path = self._folder(printer_id) / 'current.json'
        if not path.exists():
            return None
        try:
            if path.stat().st_size > 65536:
                raise ValueError()
            job = json.loads(path.read_bytes())
            if (type(job.get('schema')) is not int or job['schema'] != 1 or
                    type(job.get('printer_id')) is not int or job['printer_id'] != printer_id or job.get('state') not in
                    {'uploaded', 'attempted', 'accepted', 'rejected', 'completed', 'cleared'} or
                    not re.fullmatch('[0-9a-f]{32}', job.get('ticket', ''))):
                raise ValueError()
            return job
        except Exception:
            raise CloudError('print_state_unreadable') from None

    def save(self, job):
        folder = self._folder(job['printer_id'])
        temporary = folder / (uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(json.dumps(job, allow_nan=False).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, folder / 'current.json')
        finally:
            temporary.unlink(missing_ok=True)

    def observe_locked(self, printer):
        printer_id = identifier(printer.get('id'))
        job = self.load(printer_id)
        if job and job['state'] in {'attempted', 'accepted'}:
            if _busy(printer.get('is_printing')):
                job['seen_busy'] = True
            elif job.get('seen_busy') and _one(printer.get('device_status')) and _one(printer.get('is_printing')):
                job['state'] = 'completed'
            self.save(job)

    def observe(self, printers):
        for printer in printers:
            try:
                printer_id = identifier(printer.get('id'))
                if not (self._folder(printer_id) / 'current.json').exists():
                    continue
                with self.lock(printer_id):
                    self.observe_locked(printer)
            except CloudError:
                pass  # Status display remains available while a job owns the lock.


class PrintFlow:
    def __init__(self, client, jobs, *, clock=time.time):
        self.client, self.jobs, self.clock = client, jobs, clock

    def fresh_printer(self, printer_id):
        printers = [item for item in self.client.list_printers() if str(item.get('id')) == str(printer_id)]
        if len(printers) != 1:
            raise CloudError('printer_missing')
        printer = printers[0]
        # Do not infer hardware identity from the user-editable printer name.
        model = printer.get('model')
        if not isinstance(model, str) or model.strip().lower() not in {'kobra x', 'anycubic kobra x'}:
            raise CloudError('unsupported_printer')
        self.jobs.observe_locked(printer)
        if not _one(printer.get('device_status')):
            raise CloudError('printer_offline')
        if not _one(printer.get('is_printing')):
            raise CloudError('printer_busy_or_unknown')
        return printer

    @staticmethod
    def _unlocked(job):
        if job and job['state'] in {'attempted', 'accepted'}:
            raise CloudError('print_locked')

    def prepare(self, arguments, *, cancelled=lambda: False, progress=None):
        printer_id = identifier(arguments.get('printer_id'))
        materials = validate_materials(arguments.get('materials'))
        calibration = validate_calibration(arguments.get('calibration'))
        selections = arguments.get('slots')
        path = arguments.get('path')
        if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).suffix.lower() != '.gcode':
            raise CloudError('gcode_required')
        with self.jobs.lock(printer_id):
            printer = self.fresh_printer(printer_id)
            self._unlocked(self.jobs.load(printer_id))
            mapping = build_mapping(printer, materials, selections)
            # Freeze the sliced file before upload; never send a changing source.
            with tempfile.TemporaryDirectory(prefix='upload-', dir=self.jobs._folder(printer_id)) as temporary:
                frozen = Path(temporary) / Path(path).name
                digest = hashlib.sha256()
                try:
                    with Path(path).open('rb') as source, frozen.open('xb') as target:
                        before = os.fstat(source.fileno())
                        remaining = before.st_size
                        if remaining <= 0:
                            raise CloudError('input_empty')
                        while remaining:
                            if cancelled():
                                raise CloudError('cancelled')
                            chunk = source.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                raise CloudError('input_changed')
                            target.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                        after = os.fstat(source.fileno())
                        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                            raise CloudError('input_changed')
                except OSError:
                    raise CloudError('input_unreadable') from None
                uploaded = self.client.upload_gcode(frozen, cancelled=cancelled, progress=progress)
            printer = self.fresh_printer(printer_id)
            if build_mapping(printer, materials, selections) != mapping:
                raise CloudError('mapping_changed')
            if cancelled():
                raise CloudError('cancelled')
            job = {'schema': 1, 'ticket': uuid.uuid4().hex, 'created': self.clock(), 'state': 'uploaded',
                   'printer_id': printer_id, 'printer_name': public_printer(printer).get('name', 'Kobra X'),
                   'file_id': identifier(uploaded.file_id), 'calibration': calibration, 'mapping': mapping, 'materials': materials, 'slots': selections,
                   'sha256': digest.hexdigest(), 'account': hashlib.sha256(self.client.session.token.encode()).hexdigest()}
            self.jobs.save(job)
            return {'ticket': job['ticket'], 'printer_id': printer_id, 'printer_name': job['printer_name'],
                    'mapping': mapping, 'calibration': calibration, 'file_id': job['file_id'], 'cleanup_pending': uploaded.cleanup_pending}

    def start(self, arguments):
        if arguments.get('confirmed') is not True:
            raise CloudError('confirmation_required')
        printer_id = identifier(arguments.get('printer_id'))
        with self.jobs.lock(printer_id):
            job = self.jobs.load(printer_id)
            if not job or job['ticket'] != arguments.get('ticket'):
                raise CloudError('invalid_print_ticket')
            if job['state'] != 'uploaded':
                raise CloudError('print_already_attempted')
            if not 0 <= self.clock() - job['created'] <= 600:
                raise CloudError('print_ticket_expired')
            if hashlib.sha256(self.client.session.token.encode()).hexdigest() != job['account']:
                raise CloudError('print_account_changed')
            printer = self.fresh_printer(printer_id)
            if build_mapping(printer, validate_materials(job['materials']), job['slots']) != job['mapping']:
                raise CloudError('mapping_changed')
            # Only the reviewed, persisted options can reach the start command.
            calibration = job.get('calibration', {})
            if not isinstance(calibration, dict):
                raise CloudError('invalid_request')
            if calibration:
                if any(type(v) is not int or v not in (0, 1) for v in calibration.values()):
                    raise CloudError('invalid_request')
                calibration = validate_calibration({key: bool(value) for key, value in calibration.items()})
            # Commit the attempt BEFORE sending. A lost response is never retried.
            job['state'] = 'attempted'
            try:
                self.jobs.save(job)
            except Exception:
                raise CloudError('print_state_unreadable') from None
            try:
                self.client._api(protocol.SEND_ORDER_PATH, print_body(identifier(job['file_id']), printer_id, job['mapping'], calibration))
            except CloudError as error:
                if error.code == 'api_rejected':
                    job['state'] = 'rejected'
                    self.jobs.save(job)
                    raise
                raise CloudError('start_outcome_unknown') from None
            except Exception:
                raise CloudError('start_outcome_unknown') from None
            job['state'] = 'accepted'
            try:
                self.jobs.save(job)
            except Exception:
                raise CloudError('start_outcome_unknown') from None
            return {'accepted': True, 'printer_id': printer_id}

    def clear(self, arguments):
        if arguments.get('confirmed') is not True:
            raise CloudError('confirmation_required')
        printer_id = identifier(arguments.get('printer_id'))
        with self.jobs.lock(printer_id):
            self.fresh_printer(printer_id)
            job = self.jobs.load(printer_id)
            if job:
                job['state'] = 'cleared'
                self.jobs.save(job)
        return {'cleared': True}
