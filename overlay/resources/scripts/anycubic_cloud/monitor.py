"""Receive-only MQTT telemetry. No publish API or printer commands."""

import base64
import hashlib
import json
from pathlib import Path
import re
import ssl
import sys
import threading
import time

from .client import CloudError
from .device import _identifier, _number, _text, _light_type
from .mqtt_trust import PUBLIC_CA, PUBLIC_RSA_BLOB
from ._windows_rsa import encrypt_pkcs1

HOST = 'mqtt-universe.anycubic.com'
MAX_AGE = 60
_JOB_FIELDS = ('name', 'progress', 'remaining_minutes', 'layer', 'layers')


def subscription_state(reasons):
    if len(reasons) != 4:
        return False, False, False
    allowed = [getattr(reason, 'value', reason) in (0, 1, 2) for reason in reasons]
    # Peripheral ACL failures must not disable the existing core subscriptions.
    return allowed[0] and allowed[1], allowed[2], allowed[3]


def tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=PUBLIC_CA)
    # This broker needs legacy signature-algorithm negotiation (also required
    # by the supplied plugin). Scope it to this private-CA/hostname-verified
    # connection; allow only ECDHE/AES-GCM in TLS 1.2, normal AEAD in TLS 1.3.
    context.set_ciphers('ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-GCM-SHA256:@SECLEVEL=0')
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    return context


def credentials(session):
    if not session.email or '|' in session.email or len(session.email) > 254:
        raise CloudError('monitor_credentials_invalid')
    password = base64.b64encode(encrypt_pkcs1(PUBLIC_RSA_BLOB, session.token.encode('utf-8'))).decode('ascii')
    client_id = 'pcf_' + hashlib.md5(session.token.encode('utf-8')).hexdigest()
    signature = hashlib.md5((client_id + password + client_id).encode('utf-8')).hexdigest()
    return client_id, 'user|pcf|' + session.email + '|' + signature, password


class Reports:
    def __init__(self, printer, *, clock=time.monotonic, wall=time.time):
        key, machine = printer.get('key'), _identifier(printer.get('machine_type'))
        if (_identifier(printer.get('id')) is None or machine is None or not isinstance(key, str) or
                re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key) is None):
            raise CloudError('monitor_unavailable')
        self.printer_id = printer['id']
        self._machine, self._key = machine, key
        self._clock, self._wall = clock, wall
        self._lock = threading.Lock()
        self._fields, self._timestamps = {}, {}
        self._job_id, self._job_seen = None, False
        self._connected, self._subscribed = False, False
        self._light_subscribed, self._fan_subscribed = False, False
        self._error = ''
        self._received = self._clock()
        self._retired_jobs = []

    def filters(self):
        prefix = 'anycubic/anycubicCloud/v1/+/public/' + self._machine + '/' + self._key
        return [(prefix + '/' + channel + '/report', 0) for channel in ('status','print','light','fan')]

    def connection(self, connected, subscribed=False, error='', *, light_subscribed=True, fan_subscribed=True):
        with self._lock:
            # Reconnection must earn fresh data; old values never become live.
            if not connected or not self._connected:
                self._fields.clear()
                self._timestamps.clear()
                self._job_id, self._job_seen = None, False
                self._retired_jobs.clear()
            self._connected, self._subscribed = connected, subscribed
            self._light_subscribed = connected and subscribed and light_subscribed
            self._fan_subscribed = connected and subscribed and fan_subscribed
            if connected or error:
                self._error = error

    def _set(self, key, value):
        self._fields[key] = (value, self._received)

    def _temperatures(self, data):
        for source, key, maximum in (('curr_nozzle_temp', 'nozzle', 500), ('curr_hotbed_temp', 'bed', 200)):
            value = _number(data.get(source), maximum)
            if value is not None:
                self._set(key, value)

    def _clear_job(self, retire=True):
        if retire and self._job_id is not None:
            self._retired_jobs = (self._retired_jobs + [self._job_id])[-16:]
        for key in _JOB_FIELDS:
            self._fields.pop(key, None)
        self._fields.pop('control_job', None)
        self._job_id = None
        self._job_seen = True

    def receive(self, topic, raw, *, retained=False, duplicate=False):
        if retained or duplicate or not isinstance(raw, bytes) or len(raw) > 65536:
            return
        parts = topic.split('/') if isinstance(topic, str) else []
        if (len(parts) != 9 or parts[:3] != ['anycubic', 'anycubicCloud', 'v1'] or
                not parts[3] or parts[4:7] != ['public', self._machine, self._key] or
                parts[7] not in ('status', 'print', 'light', 'fan') or parts[8] != 'report'):
            return
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return
        if not isinstance(payload, dict) or payload.get('code', 200) != 200:
            return
        kind, action, state = payload.get('type'), payload.get('action'), payload.get('state')
        if kind in (None, '') and parts[7] in ('light', 'fan'):
            kind = parts[7]
        if not all(isinstance(value, str) for value in (kind, action, state)):
            return
        if parts[7] in ('light','fan') and kind != parts[7]:
            return
        stamp = None
        if 'timestamp' in payload:
            stamp = _number(payload['timestamp'], 4102444800000)
            if stamp is None:
                return
            if stamp > 100000000000:
                stamp /= 1000
            if self._wall() - stamp > MAX_AGE or stamp - self._wall() > 30:
                return
        data = payload.get('data')
        if kind == 'light' and isinstance(data, list):
            data = {'lights':data}
        data = data if isinstance(data, dict) else {}
        with self._lock:
            if not self._connected or not self._subscribed:
                return
            self._received = self._clock() - max(0, self._wall() - stamp) if stamp is not None else self._clock()
            if stamp is not None:
                if stamp < self._timestamps.get(kind, 0):
                    return
                self._timestamps[kind] = stamp
            if kind in ('tempature', 'temperature') and action in ('auto', 'query') and state == 'done':
                self._temperatures(data)
            elif kind == 'fan' and action in ('auto', 'query', 'report', 'reportInfo') and state in ('done', 'success'):
                value = data.get('fan_speed_pct')
                if type(value) is int and 0 <= value <= 100:
                    self._set('part_fan', value)
            elif kind == 'light' and action in ('auto', 'query', 'control', 'report', 'reportInfo') and state in ('done', 'success'):
                lights = data.get('lights') if isinstance(data.get('lights'), list) else [data]
                # Do not select from a truncated list: another supported light
                # beyond the bound would make the selection ambiguous.
                candidates = [light for light in (lights if len(lights) <= 16 else []) if isinstance(light, dict) and
                              _light_type(light.get('type')) is not None]
                # An ambiguous list must not make a random light controllable.
                self._fields.pop('light_type', None)
                self._fields.pop('light_on', None)
                self._fields.pop('light_report_problem', None)
                if len(lights) == 1 and len(candidates) == 1:
                    self._set('light_type', _light_type(candidates[0]['type']))
                    status = candidates[0].get('status')
                    # Identity and observed state are independent. Explicit On
                    # and Off do not need a guessed current state to work.
                    if type(status) is bool:
                        self._set('light_on', status)
                    elif type(status) in (int, str) and str(status) in ('0', '1'):
                        self._set('light_on', str(status) == '1')
                else:
                    self._set('light_report_problem', 'ambiguous' if len(lights) > 1 else 'unsupported')
            elif kind == 'status' and action == 'workReport' and state in ('free', 'busy'):
                if state == 'free':
                    self._clear_job()
                self._set('is_printing', 1 if state == 'free' else 2)
            elif kind == 'print':
                incoming = _identifier(data.get('taskid'))
                if incoming is not None and incoming in self._retired_jobs:
                    return
                if (action in ('start', 'stop') and state in ('finished', 'stoped', 'stopped', 'stopping')):
                    if incoming is not None and self._job_id is not None and incoming != self._job_id:
                        return
                    self._clear_job()
                    self._set('is_printing', 1)
                    return
                if not ((action in ('start', 'update') and state in ('updated', 'printing', 'preheating', 'downloading', 'checking')) or
                        (action in ('pause', 'resume') and state in ('paused', 'pausing', 'resumed', 'resuming'))):
                    return
                layer = _number(data.get('curr_layer'), 1000000)
                old_layer = self._fields.get('layer', (None, 0))[0]
                if incoming is not None and self._job_id != incoming:
                    self._clear_job()
                elif layer is not None and old_layer is not None and layer < old_layer:
                    self._clear_job(retire=False)
                if incoming is not None:
                    self._job_id = incoming
                    phase = {'printing':'printing','paused':'paused','resumed':'printing'}.get(state,'unknown')
                    self._set('control_job', {'project_id':int(incoming),'phase':phase})
                else:
                    self._fields.pop('control_job', None)
                self._job_seen = True
                self._set('is_printing', 2)
                self._temperatures(data)
                for source, key, maximum in (('progress', 'progress', 100), ('remain_time', 'remaining_minutes', 525600),
                                              ('curr_layer', 'layer', 1000000), ('total_layers', 'layers', 1000000)):
                    value = _number(data.get(source), maximum)
                    if value is not None and (key not in ('layer', 'layers') or value == int(value)):
                        self._set(key, value)
                name = _text(data.get('filename'), 256)
                if name:
                    self._set('name', name)

    def snapshot(self):
        with self._lock:
            now = self._clock()
            fields = {key: value for key, (value, received) in self._fields.items()
                      if 0 <= now - received <= (10 if key == 'control_job' else MAX_AGE) and self._connected and self._subscribed}
            control_job = fields.pop('control_job', None)
            if 'layer' in fields and 'layers' in fields and fields['layer'] > fields['layers']:
                fields.pop('layer')
                fields.pop('layers')
            state = ('connecting' if not self._connected else 'waiting' if not self._subscribed else
                     'live' if fields else 'stale' if self._fields else 'waiting')
            result = {'printer_id': self.printer_id, 'state': state, 'fields': fields,
                      'report_age_seconds': int(max(0, now - self._received)),
                      'job_seen': self._job_seen, 'connected': self._connected and self._subscribed,
                      'light_reports_subscribed': self._light_subscribed,
                      'fan_reports_subscribed': self._fan_subscribed,
                      'error': self._error}
            if control_job is not None:
                result['control_job'] = control_job
            return result


def run_monitor(client, wanted, *, cancelled, update, mqtt_factory=None, duration=270):
    if _identifier(wanted) is None:
        raise CloudError('invalid_request')
    printer = next((p for p in client.list_printers() if _identifier(p.get('id')) == _identifier(wanted)), None)
    if printer is None:
        raise CloudError('monitor_unavailable')
    reports = Reports(printer)
    if cancelled():
        raise CloudError('cancelled')
    if mqtt_factory is None:
        # Reuse the application's existing, licensed Paho distribution.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))
        import paho.mqtt.client as mqtt
        mqtt_factory = lambda identity: mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
            client_id=identity, protocol=mqtt.MQTTv311, clean_session=True)
    identity, username, password = credentials(client.session)
    connection = mqtt_factory(identity)
    connection.username_pw_set(username, password)
    connection.tls_set_context(tls_context())
    connection.connect_timeout = 8
    connection.reconnect_delay_set(min_delay=2, max_delay=30)

    def connected(mqtt, userdata, flags, reason, properties=None):
        accepted = getattr(reason, 'value', reason) == 0
        reports.connection(accepted, error='' if accepted else 'monitor_auth_rejected')
        if accepted:
            mqtt.subscribe(reports.filters())

    def subscribed(mqtt, userdata, mid, reasons, properties=None):
        accepted, light, fan = subscription_state(reasons)
        reports.connection(True, accepted, '' if accepted else 'monitor_subscription_rejected',
                           light_subscribed=light, fan_subscribed=fan)

    def disconnected(mqtt, userdata, flags, reason, properties=None):
        reports.connection(False)

    connection.on_connect = connected
    connection.on_subscribe = subscribed
    connection.on_disconnect = disconnected
    connection.on_message = lambda mqtt, userdata, msg: reports.receive(msg.topic, msg.payload,
        retained=msg.retain, duplicate=msg.dup)
    started = False
    try:
        update(reports.snapshot())
        connection.connect(HOST, 8883, keepalive=30)
        connection.loop_start()
        started = True
        deadline = time.monotonic() + duration
        wait = threading.Event()
        while time.monotonic() < deadline and not cancelled():
            update(reports.snapshot())
            wait.wait(1)
        return {'stopped': True}
    except ssl.SSLError:
        raise CloudError('monitor_tls_failed') from None
    except CloudError:
        raise
    except Exception:
        raise CloudError('monitor_connection_failed') from None
    finally:
        try:
            connection.disconnect()
        except Exception:
            pass
        if started:
            try:
                connection.loop_stop()
            except Exception:
                pass
