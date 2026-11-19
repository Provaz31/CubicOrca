"""Explicitly requested Agora camera lease; account credentials never leave here."""

import base64
import re
import threading

from .client import CloudError
from .device import _identifier
from . import protocol


def viewer_config(data):
    block = data.get('shengwang') if isinstance(data, dict) else None
    if not isinstance(block, dict) or not block:
        raise CloudError('camera_transport_unavailable')
    app = block.get('appid')
    channel, token = block.get('channel'), block.get('rtc_token')
    uid = _identifier(block.get('client_uid') or block.get('uid'))
    if (not isinstance(app, str) or re.fullmatch(r'[0-9a-fA-F]{32}', app) is None or
            not isinstance(channel, str) or not 1 <= len(channel) <= 256 or not channel.isascii() or
            not channel.isprintable() or not isinstance(token, str) or not 1 <= len(token) <= 8192 or
            re.fullmatch(r'[A-Za-z0-9_+/=.\-]+', token) is None or uid is None or int(uid) >= 2**32):
        raise CloudError('camera_credentials_invalid')
    mode = block.get('encryption_mode') or 'none'
    if not isinstance(mode, str):
        raise CloudError('camera_credentials_invalid')
    mode = mode.lower().replace('_', '-')
    if mode not in ('none', 'aes-128-xts', 'aes-256-xts', 'aes-128-ecb', 'sm4-128-ecb',
                    'aes-128-gcm', 'aes-256-gcm', 'aes-128-gcm2', 'aes-256-gcm2'):
        raise CloudError('camera_credentials_invalid')
    key = block.get('encryption_key') or ''
    salt = block.get('encryption_kdf_salt') or ''
    if (not isinstance(key, str) or not key.isascii() or not key.isprintable() and key != '' or
            len(key) > 62 or mode != 'none' and not key or mode == 'none' and key):
        raise CloudError('camera_credentials_invalid')
    if mode.endswith('gcm2'):
        try:
            if not isinstance(salt, str) or len(salt) > 64 or len(base64.b64decode(salt, validate=True)) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise CloudError('camera_credentials_invalid') from None
    else:
        salt = ''
    device = data.get('shengwang_device')
    device_uid = _identifier(device.get('uid')) if isinstance(device, dict) else None
    if device_uid is not None and int(device_uid) >= 2**32:
        raise CloudError('camera_credentials_invalid')
    return {'appid': app, 'channel': channel, 'token': token, 'uid': int(uid),
            'device_uid': int(device_uid) if device_uid else None,
            'encryption_mode': mode, 'encryption_key': key, 'salt': salt}


def run_camera(client, wanted, *, confirmed, cancelled, update, wait=None):
    if confirmed is not True or _identifier(wanted) is None:
        raise CloudError('invalid_request')
    if cancelled():
        raise CloudError('cancelled')
    printer = next((item for item in client.list_printers()
                    if _identifier(item.get('id')) == _identifier(wanted)), None)
    if printer is None or str(printer.get('device_status')) != '1':
        raise CloudError('camera_unavailable')
    if cancelled():
        raise CloudError('cancelled')
    owned, cleanup_pending = False, False
    failure = None
    result = {'stopped': True}
    try:
        try:
            data = client._api(protocol.SEND_ORDER_PATH,
                {'printer_id': int(wanted), 'order_id': '1001', 'shengwang_rtc_support': True}, timeout=10)
        except CloudError as exc:
            raise CloudError('camera_open_unknown' if exc.code in ('connection_failed', 'http_error') else
                             'camera_unavailable') from None
        # No credential block often means another application owns the camera.
        # Never send a global camera-close order for that other session.
        owned = isinstance(data, dict) and bool(data.get('shengwang') or data.get('token'))
        config = viewer_config(data)
        if not cancelled():
            update({'viewer': config})  # Short-lived viewer secrets, private pipe only.
            sleeper = wait if wait is not None else threading.Event().wait
            while not cancelled():
                sleeper(0.2)
    except CloudError as exc:
        failure = exc.code
    except Exception:
        failure = 'camera_failed'
    finally:
        if owned:
            try:
                client._api(protocol.SEND_ORDER_PATH, {'printer_id': int(wanted), 'order_id': 1002}, timeout=8)
            except Exception:
                cleanup_pending = True
        result['cleanup_pending'] = cleanup_pending
    if failure:
        raise CloudError(failure, cleanup_pending=cleanup_pending)
    return result
