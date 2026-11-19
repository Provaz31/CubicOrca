"""Single bounded axis orders; no continuous motion, raw G-code or extrusion."""

from .client import CloudError
from .printing import PrintFlow, identifier
from . import protocol


def control(client, jobs, arguments, *, cancelled=lambda: False):
    if (not isinstance(arguments, dict) or
            set(arguments) != {'printer_id', 'axis', 'direction', 'distance', 'confirmed'} or
            arguments.get('confirmed') is not True):
        raise CloudError('invalid_request')
    wanted = identifier(arguments['printer_id'])
    axis, direction, distance = arguments['axis'], arguments['direction'], arguments['distance']
    if (axis not in ('X', 'Y', 'Z', 'ALL') or direction not in ('minus', 'plus', 'home') or
            type(distance) is not int or
            (direction == 'home' and distance != 0) or
            (direction != 'home' and (axis == 'ALL' or distance not in (1, 5, 10)))):
        raise CloudError('invalid_motion')
    if cancelled():
        raise CloudError('cancelled')
    with jobs.lock(wanted):
        flow = PrintFlow(client, jobs)
        flow.fresh_printer(wanted)
        flow._unlocked(jobs.load(wanted))
        if cancelled():
            raise CloudError('cancelled')
        # Order 201 is a finite-distance firmware operation, not a held button.
        # Plus/minus refer to machine coordinates; do not invert bed motion.
        body = {'printer_id': wanted, 'order_id': '201', 'data': {
            'axis': {'X':1, 'Y':2, 'Z':3, 'ALL':4}[axis],
            'move_type': {'minus':0, 'plus':1, 'home':2}[direction],
            'distance': distance}}
        try:
            client._api(protocol.SEND_ORDER_PATH, body, timeout=10)
        except CloudError as exc:
            if exc.code == 'api_rejected':
                raise
            raise CloudError('motion_unconfirmed') from None
        return {'accepted':True}
