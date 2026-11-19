"""Read-only, bounded cloud-library metadata. Never forward storage URLs."""

from datetime import datetime, timezone
import re
from urllib.parse import unquote

from .client import CloudError
from .device import _identifier


def _name(value):
    if not isinstance(value, str) or len(value) > 1024:
        return ''
    # Filenames are untrusted too: a URL or a credential pasted into a name
    # must not become visible merely because its containing key is allowed.
    scan = value
    for _ in range(3):
        decoded = unquote(scan)
        if decoded == scan:
            break
        scan = decoded
    lower = scan.lower()
    if (any(marker in lower for marker in ('://','?','#','x-amz-','access_token=','bearer ')) or
            re.search(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}',scan)):
        return ''
    text = ''.join(c for c in value if c.isprintable()).replace('\\','/').rsplit('/',1)[-1]
    return text[:256]


def _size(value):
    if type(value) not in (int,str) or not re.fullmatch(r'[0-9]{1,19}',str(value)):
        return None
    number = int(value)
    return number if number < 2**63 else None


def _date(value):
    number = _size(value)
    if number is not None:
        if 946684800000 <= number <= 4102444800000:
            number /= 1000
        if 946684800 <= number <= 4102444800:
            return datetime.fromtimestamp(number,timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        return ''
    if not isinstance(value,str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?',value):
        return ''
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        if not 2000 <= parsed.year <= 2100:
            return ''
        if len(value) == 10:
            return parsed.date().isoformat()
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        # Do not invent the timezone of a server's naive calendar value.
        return parsed.isoformat(sep=' ',timespec='seconds')
    except ValueError:
        return ''


def list_page(client, arguments, *, cancelled=lambda: False):
    if (not isinstance(arguments,dict) or set(arguments) != {'page'} or
            type(arguments['page']) is not int or not 1 <= arguments['page'] <= 10000):
        raise CloudError('invalid_request')
    if cancelled():
        raise CloudError('cancelled')
    records = client.cloud_files(arguments['page'])
    if cancelled():
        raise CloudError('cancelled')
    if not isinstance(records,list) or len(records) > 20:
        raise CloudError('invalid_response')
    files, seen = [], set()
    for record in records:
        key = _identifier(record.get('id')) if isinstance(record,dict) else None
        if key is None or key in seen:
            raise CloudError('invalid_response')
        seen.add(key)
        name = _name(record.get('old_filename')) or _name(record.get('filename'))
        extension = record.get('file_extension')
        if not isinstance(extension,str) or not re.fullmatch(r'\.?[a-zA-Z0-9]{1,12}',extension):
            extension = name.rsplit('.',1)[-1] if '.' in name else ''
        if not re.fullmatch(r'\.?[a-zA-Z0-9]{1,12}',extension):
            extension = ''
        files.append({'id':int(key),'name':name,'size':_size(record.get('size')),
                      'uploaded_at':_date(record.get('time')),'extension':extension.lstrip('.').lower()})
    return {'files':files,'page':arguments['page'],'page_size':20,
            'has_next':len(records) == 20 and arguments['page'] < 10000}
