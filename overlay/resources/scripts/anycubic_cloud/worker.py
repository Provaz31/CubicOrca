"""Private stdio protocol for the native host. Not a plugin or web endpoint.

The host sends one JSON request on a private pipe and keeps stdin open until
completion. A subsequent {"method":"cancel"} line cancels a streaming upload.
Neither account tokens nor signed upload URLs are included in replies.
"""

import json
import os
from pathlib import Path
import sys
import threading

from .client import CloudClient, CloudError
from .session_store import SessionStore
from .device import public_printer, public_telemetry, _identifier
from .printing import PrintFlow, PrintJobs

_METHODS = {"account", "exchange_session", "import_session", "logout", "list_printers", "workbench_snapshot", "monitor", "camera", "upload_gcode", "prepare_print", "start_print", "clear_print_lock", "set_temperature", "peripheral_control", "review_job_control", "execute_job_control", "cloud_library", "motion_control", "job_preview", "review_update", "extract_update"}


def handle_request(request, *, cancelled=lambda: False, progress=None,
                   update=lambda data: None, client_factory=CloudClient, store_factory=SessionStore):
    if (not isinstance(request, dict) or request.get("method") not in _METHODS or
            not isinstance(request.get("session_path"), str) or
            not Path(request["session_path"]).is_absolute()):
        raise CloudError("invalid_request")
    arguments = request.get("args", {})
    if not isinstance(arguments, dict):
        raise CloudError("invalid_request")
    if request["method"] in {"start_print", "clear_print_lock", "camera", "set_temperature", "peripheral_control", "execute_job_control", "motion_control"} and arguments.get("confirmed") is not True:
        raise CloudError("invalid_request")
    if request['method'] in ('review_update','extract_update'):
        from .portable_update import handle
        return handle(arguments,extract=request['method']=='extract_update',cancelled=cancelled)
    store = store_factory(request["session_path"])
    method = request["method"]
    if method == "import_session":
        path = arguments.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise CloudError("invalid_request")
        session = store.import_existing(path)
        return {"signed_in": True, "email": session.email}
    if method == "logout":
        store.clear()
        return {"signed_in": False}
    if method == "exchange_session":
        if (not isinstance(arguments.get("access_token"), str) or not arguments["access_token"] or
                not isinstance(arguments.get("client_id"), str) or not arguments["client_id"]):
            raise CloudError("invalid_request")
        client = client_factory()
        session = client.exchange_session(arguments["access_token"], arguments["client_id"])
        store.save(session)
        return {"signed_in": True, "email": session.email}
    session = store.load()
    if method == "account":
        return {"signed_in": session is not None, "email": session.email if session else ""}
    if session is None:
        raise CloudError("sign_in_required")
    client = client_factory(session=session)
    if method == 'job_preview':
        from .job_preview import load
        return load(client,arguments,cancelled=cancelled)
    if method == 'camera':
        from .camera import run_camera
        return run_camera(client, arguments.get('printer_id'), confirmed=arguments.get('confirmed'),
                          cancelled=cancelled, update=update)
    if method == 'monitor':
        from .monitor import run_monitor
        return run_monitor(client, arguments.get('printer_id'), cancelled=cancelled, update=update)
    if method == "cloud_library":
        from .library import list_page
        return list_page(client, arguments, cancelled=cancelled)
    jobs = PrintJobs(Path(request["session_path"]).parent.parent / "print-jobs")
    if method in {"review_job_control", "execute_job_control"}:
        from .job_control import review, execute
        operation = review if method == "review_job_control" else execute
        return operation(client, jobs, arguments, cancelled=cancelled)
    if method == "peripheral_control":
        from .peripherals import control
        return control(client, jobs, arguments, cancelled=cancelled)
    if method == "motion_control":
        from .motion import control
        return control(client, jobs, arguments, cancelled=cancelled)
    if method == "set_temperature":
        from .temperature import set_temperature
        return set_temperature(client, jobs, arguments, cancelled=cancelled)
    if method == "prepare_print":
        return PrintFlow(client, jobs).prepare(arguments, cancelled=cancelled, progress=progress)
    if method == "start_print":
        return PrintFlow(client, jobs).start(arguments)
    if method == "clear_print_lock":
        return PrintFlow(client, jobs).clear(arguments)
    if method in {"list_printers", "workbench_snapshot"}:
        # Do not forward an arbitrary service record to JavaScript/native logs.
        # Device metadata is an allowlist; account and connection secrets stay here.
        raw = client.list_printers()
        jobs.observe(raw)
        printers = [public_printer(item) for item in raw]
        if method == "workbench_snapshot":
            wanted = arguments.get('printer_id')
            if wanted is not None and _identifier(wanted) is None:
                raise CloudError('invalid_request')
            if wanted is None and len(raw) == 1:
                wanted = raw[0].get('id')
            for original, printer in zip(raw, printers):
                if _identifier(wanted) is None or _identifier(original.get('id')) != _identifier(wanted):
                    continue
                telemetry = {'detail_loaded': False, 'projects_loaded': False}
                detail, projects = original, []
                if cancelled():
                    raise CloudError('cancelled')
                try:
                    detail = client.printer_info(wanted)
                    telemetry['detail_loaded'] = True
                except CloudError:
                    pass  # Keep metadata and show the missing section inline.
                if cancelled():
                    raise CloudError('cancelled')
                if (str(detail.get('device_status')) == '1' and str(detail.get('is_printing')) == '2'):
                    try:
                        projects = client.recent_projects()
                        telemetry['projects_loaded'] = True
                    except CloudError:
                        pass
                # Detail is for this ID only. Refresh metadata from the richer
                # record without dropping feeder fields absent from that API.
                printer.update(public_printer({**original, **detail}))
                telemetry.update(public_telemetry(detail, projects))
                printer['telemetry'] = telemetry
                break
            if cancelled():
                raise CloudError('cancelled')
        return {"printers": printers}
    path = arguments.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise CloudError("invalid_request")
    result = client.upload_gcode(path, cancelled=cancelled, progress=progress)
    return {"file_id": result.file_id, "size": result.size, "cleanup_pending": result.cleanup_pending}


def _emit(payload):
    line = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(line) > 2 * 1024 * 1024:
        raise CloudError("response_too_large")
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _read_line(limit):
    # Avoid a daemon thread holding Python's buffered stdin lock at interpreter
    # shutdown. Raw reads also avoid prefetching the later cancellation message.
    data = bytearray()
    while len(data) <= limit:
        value = os.read(sys.stdin.fileno(), 1)
        if not value:
            return bytes(data)
        data.extend(value)
        if value == b"\n":
            break
    return bytes(data)


def main():
    try:
        line = _read_line(131072)
        if not line.endswith(b"\n") or len(line) > 131072:
            raise CloudError("invalid_request")
        request = json.loads(line)
        stop = threading.Event()
        if isinstance(request, dict) and request.get("method") in {"upload_gcode", "prepare_print", "monitor", "camera"}:
            def watch_cancel():
                try:
                    command = _read_line(4096)
                    if not command or json.loads(command).get("method") == "cancel":
                        stop.set()
                except Exception:
                    stop.set()
            threading.Thread(target=watch_cancel, daemon=True).start()
        result = handle_request(request, cancelled=stop.is_set,
            update=lambda data: _emit({'type': 'update', 'data': data}),
            progress=lambda phase, fraction: _emit({"type": "progress", "phase": phase, "fraction": fraction}))
        _emit({"type": "result", "ok": True, "data": result})
    except CloudError as exc:
        reply = {"type": "result", "ok": False, "error": exc.code, "cleanup_pending": exc.cleanup_pending}
        if exc.api_code is not None:
            reply['api_code'] = exc.api_code
        _emit(reply)
    except Exception:
        _emit({"type": "result", "ok": False, "error": "operation_failed"})


if __name__ == "__main__":
    main()
