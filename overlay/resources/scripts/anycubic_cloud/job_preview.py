"""Read-only preview of one verified active job. Never forward a storage URL."""
import base64
import struct
from urllib.parse import urlsplit
from .client import CloudError
from .printing import identifier, _one
from .device import _object

HOST = 'workbentch.s3.us-east-2.amazonaws.com'


def image_size(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24 and data[12:16] == b'IHDR':
        return struct.unpack('>II', data[16:24])
    if data.startswith(b'\xff\xd8'):
        index = 2
        while index + 4 <= len(data):
            if data[index] != 255: break
            marker = data[index + 1]
            index += 2
            if marker == 255: index -= 1; continue
            if marker in (0xd8, 0xd9): continue
            length = int.from_bytes(data[index:index+2], 'big')
            if length < 2 or index + length > len(data): break
            if marker in (0xc0, 0xc1, 0xc2) and length >= 8:
                height, width = struct.unpack('>HH', data[index+3:index+7])
                return width, height
            index += length
    raise CloudError('preview_unavailable')


def load(client, arguments, *, cancelled=lambda: False):
    if not isinstance(arguments, dict) or set(arguments) != {'printer_id', 'project_id'}:
        raise CloudError('invalid_request')
    wanted, job_id = identifier(arguments['printer_id']), identifier(arguments['project_id'])
    if cancelled(): raise CloudError('cancelled')
    matches = [p for p in client.list_printers() if str(p.get('id')) == str(wanted)]
    if len(matches) != 1 or not _one(matches[0].get('device_status')) or str(matches[0].get('is_printing')) != '2':
        raise CloudError('preview_unavailable')
    projects = [p for p in client.recent_projects() if str(p.get('printer_id')) == str(wanted) and
                type(p.get('print_status')) in (int, str) and str(p['print_status']) in ('1','4','5','6','7')]
    if len(projects) != 1 or str(projects[0].get('id')) != str(job_id):
        raise CloudError('preview_unavailable')
    project = projects[0]
    url = project.get('img')
    if not url:
        key = _object(project.get('slice_param')).get('image_id')
        url = 'https://' + HOST + '/' + key if isinstance(key, str) and key and not key.startswith('/') and '://' not in key else ''
    try:
        parts = urlsplit(url) if isinstance(url, str) and len(url) <= 8192 else None
        if (parts is None or parts.scheme != 'https' or parts.hostname != HOST or
                parts.port not in (None,443) or parts.username is not None or parts.password is not None or parts.fragment):
            raise ValueError()
    except (ValueError, TypeError): raise CloudError('preview_unavailable') from None
    if cancelled(): raise CloudError('cancelled')
    data = client._transport.request('GET', url, None, {'Accept':'image/png,image/jpeg'}, 8)
    if not isinstance(data, bytes) or not 0 < len(data) <= 256*1024:
        raise CloudError('preview_unavailable')
    width, height = image_size(data)
    if not 0 < width <= 1024 or not 0 < height <= 1024:
        raise CloudError('preview_unavailable')
    if cancelled(): raise CloudError('cancelled')
    return {'project_id':job_id, 'image_base64':base64.b64encode(data).decode('ascii')}
