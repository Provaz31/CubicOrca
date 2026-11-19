"""Confirmed, bounded Kobra X preheat commands. No automatic retries."""

from .client import CloudError
from .printing import PrintFlow, identifier
from . import protocol


def set_temperature(client, jobs, arguments, *, cancelled=lambda: False):
    if (not isinstance(arguments, dict) or
            set(arguments) != {'printer_id', 'heater', 'target', 'confirmed'} or
            arguments.get('confirmed') is not True):
        raise CloudError('invalid_request')
    wanted = identifier(arguments['printer_id'])
    heater, target = arguments['heater'], arguments['target']
    if (heater not in ('nozzle', 'bed', 'both') or type(target) is not int or
            not 0 <= target <= (300 if heater == 'nozzle' else 100) or
            heater == 'both' and target != 0):
        raise CloudError('invalid_temperature')
    if cancelled():
        raise CloudError('cancelled')
    # Share the print-start lock, including its ambiguous-start protection.
    # Re-read identity and idle/online state rather than trusting GUI snapshots.
    with jobs.lock(wanted):
        flow = PrintFlow(client, jobs)
        flow.fresh_printer(wanted)
        flow._unlocked(jobs.load(wanted))
        if cancelled():
            raise CloudError('cancelled')
        body = {'printer_id': wanted, 'order_id': '1216', 'data': {
            'type': {'nozzle': 0, 'bed': 1, 'both': 2}[heater],
            'target_nozzle_temp': target if heater == 'nozzle' else 0,
            'target_hotbed_temp': target if heater == 'bed' else 0}}
        try:
            client._api(protocol.SEND_ORDER_PATH, body, timeout=10)
        except CloudError as exc:
            if exc.code == 'api_rejected':
                raise
            # The command may have reached the printer. Never send it again
            # automatically or describe a timeout as a definite rejection.
            raise CloudError('temperature_unconfirmed') from None
        return {'accepted': True}  # Cloud ACK, not an attained temperature.
