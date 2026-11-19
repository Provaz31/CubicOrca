"""Explicit Kobra X part-cooling fan and reported-light controls."""

from .client import CloudError
from .printing import PrintFlow, identifier, _one
from .device import _light_type
from . import protocol


def control(client, jobs, arguments, *, cancelled=lambda: False):
    if not isinstance(arguments, dict) or arguments.get('confirmed') is not True:
        raise CloudError('invalid_request')
    action = arguments.get('action')
    required = {'printer_id', 'action', 'confirmed'}
    if action == 'fan':
        required.add('value')
        value = arguments.get('value')
        if type(value) is not int or not 0 <= value <= 100:
            raise CloudError('invalid_request')
        order, data = '1221', {'fan_speed_pct': value}
    elif action == 'light':
        required.update(('value', 'light_type'))
        value, kind = arguments.get('value'), _light_type(arguments.get('light_type'))
        if type(value) is not bool or kind is None:
            raise CloudError('light_unavailable')
        # No guessed default light type. GUI supplies an observed, fresh type.
        order, data = '1233', {'type':kind, 'status':int(value), 'brightness':100 if value else 0}
    elif action == 'query_light':
        order, data = '1232', None
    else:
        raise CloudError('invalid_request')
    if set(arguments) != required:
        raise CloudError('invalid_request')
    wanted = identifier(arguments['printer_id'])
    if cancelled():
        raise CloudError('cancelled')
    with jobs.lock(wanted):
        if action == 'fan':
            flow = PrintFlow(client, jobs)
            flow.fresh_printer(wanted)
            flow._unlocked(jobs.load(wanted))
        else:
            matches = [p for p in client.list_printers() if str(p.get('id')) == str(wanted)]
            if len(matches) != 1:
                raise CloudError('printer_missing')
            printer = matches[0]
            model = printer.get('model')
            if not isinstance(model, str) or model.strip().lower() not in ('kobra x', 'anycubic kobra x'):
                raise CloudError('unsupported_printer')
            if not _one(printer.get('device_status')):
                raise CloudError('printer_offline')
            state = printer.get('is_printing')
            if type(state) not in (int, str) or str(state) not in ('1', '2'):
                raise CloudError('printer_busy_or_unknown')
        if cancelled():
            raise CloudError('cancelled')
        body = {'printer_id':wanted, 'order_id':order}
        if data is not None:
            body['data'] = data
        try:
            client._api(protocol.SEND_ORDER_PATH, body, timeout=10)
        except CloudError as exc:
            if exc.code == 'api_rejected':
                raise
            raise CloudError('peripheral_unconfirmed') from None
        return {'accepted':True}  # Never synthesize an observed fan/light state.
