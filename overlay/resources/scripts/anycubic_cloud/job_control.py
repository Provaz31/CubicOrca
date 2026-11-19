"""Reviewed, single-use controls for an explicitly identified running job."""

import json
import math
import os
import re
import time
import uuid

from .client import CloudError
from .device import _text
from .printing import identifier, _one
from . import protocol

ORDERS = {'pause':2, 'resume':3, 'stop':4}


def _fresh_job(client, wanted, project_id, action):
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
    if type(state) not in (int, str) or str(state) != '2':
        raise CloudError('job_changed')
    active = [p for p in client.recent_projects() if str(p.get('printer_id')) == str(wanted) and
              type(p.get('print_status')) in (int, str) and str(p['print_status']) in ('1','4','5','6','7')]
    # The expected ID comes from this printer's recent live task report.
    # Never choose a project by its position in historical cloud results.
    if len(active) != 1 or str(active[0].get('id')) != str(project_id) or str(active[0]['print_status']) != '1':
        raise CloudError('job_changed')
    project = active[0]
    paused = project.get('pause')
    if type(paused) not in (int, str) or str(paused) not in ('0', '1'):
        raise CloudError('job_state_unknown')
    paused = int(paused)
    if action == 'pause' and paused != 0 or action == 'resume' and paused != 1:
        raise CloudError('job_changed')
    return printer, project, paused


def _read(path):
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 4096:
            raise ValueError()
        record = json.loads(path.read_bytes())
        if (not isinstance(record, dict) or type(record.get('schema')) is not int or record['schema'] != 1 or
                type(record.get('printer_id')) is not int or identifier(record['printer_id']) != record['printer_id'] or
                type(record.get('project_id')) is not int or identifier(record['project_id']) != record['project_id'] or
                record.get('action') not in ORDERS or type(record.get('paused')) is not int or record['paused'] not in (0,1) or
                record.get('state') not in ('prepared','attempted','accepted','rejected') or
                not isinstance(record.get('ticket'), str) or not re.fullmatch('[0-9a-f]{32}',record['ticket']) or
                type(record.get('created')) not in (int,float) or not math.isfinite(record['created'])):
            raise ValueError()
        return record
    except Exception:
        raise CloudError('job_control_state_unreadable') from None


def _write(path, record):
    temporary = path.with_name(uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(json.dumps(record,allow_nan=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def review(client, jobs, arguments, *, cancelled=lambda: False, clock=time.time):
    if (not isinstance(arguments, dict) or set(arguments) != {'printer_id','project_id','action'} or
            not isinstance(arguments['action'],str) or arguments['action'] not in ORDERS):
        raise CloudError('invalid_request')
    wanted, project_id = identifier(arguments['printer_id']), identifier(arguments['project_id'])
    if cancelled():
        raise CloudError('cancelled')
    with jobs.lock(wanted):
        printer, project, paused = _fresh_job(client,wanted,project_id,arguments['action'])
        path = jobs._folder(wanted)/'job-control.json'
        previous = _read(path)
        if previous and previous['printer_id'] == wanted and previous['project_id'] == project_id and previous['state'] in ('attempted','accepted'):
            # A new confirmation cannot bypass an uncertain previous command.
            # Only an observed target state resolves pause/resume protection.
            resolved = (previous['action'] == 'pause' and paused == 1 or
                        previous['action'] == 'resume' and paused == 0)
            if not resolved:
                raise CloudError('job_control_pending')
        if cancelled():
            raise CloudError('cancelled')
        record = {'schema':1,'printer_id':wanted,'project_id':project_id,'action':arguments['action'],
                  'paused':paused,'ticket':uuid.uuid4().hex,'state':'prepared','created':clock()}
        _write(path,record)
        return {'ticket':record['ticket'],'project_id':project_id,'action':record['action'],
                'printer_name':_text(printer.get('name'),128), 'job_name':_text(project.get('gcode_name'),256)}


def execute(client, jobs, arguments, *, cancelled=lambda: False, clock=time.time):
    if (not isinstance(arguments,dict) or set(arguments) != {'printer_id','ticket','confirmed'} or
            arguments['confirmed'] is not True or not isinstance(arguments['ticket'],str) or
            not re.fullmatch('[0-9a-f]{32}',arguments['ticket'])):
        raise CloudError('invalid_request')
    wanted = identifier(arguments['printer_id'])
    if cancelled():
        raise CloudError('cancelled')
    with jobs.lock(wanted):
        path = jobs._folder(wanted)/'job-control.json'
        record = _read(path)
        if not record or record['printer_id'] != wanted or record['ticket'] != arguments['ticket'] or record['state'] != 'prepared':
            raise CloudError('job_control_consumed')
        if not 0 <= clock()-record['created'] <= 90:
            raise CloudError('job_control_expired')
        _, _, paused = _fresh_job(client,wanted,record['project_id'],record['action'])
        if paused != record['paused']:
            raise CloudError('job_changed')
        if cancelled():
            raise CloudError('cancelled')
        if not 0 <= clock()-record['created'] <= 90:
            raise CloudError('job_control_expired')
        record['state'] = 'attempted'
        _write(path,record)  # Consume durably BEFORE any physical command.
        body = {'order_id':ORDERS[record['action']], 'printer_id':wanted, 'project_id':record['project_id'],
                'data':None,'ams_info':None,'settings':None}
        try:
            client._api(protocol.SEND_ORDER_PATH,body,timeout=10)
        except CloudError as exc:
            if exc.code == 'api_rejected':
                record['state'] = 'rejected'
                _write(path,record)
                raise
            raise CloudError('job_control_unconfirmed') from None
        record['state'] = 'accepted'
        _write(path,record)
        return {'accepted':True}
