"""Cloud account and upload operations for the built-in Anycubic provider.

Importing this module performs no I/O. The native host owns account persistence,
printer selection and UI confirmation. Print commands are submitted only by the separate confirmed-print workflow.
"""

from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import time
from urllib.parse import urlsplit
import uuid

from . import protocol


class CloudError(Exception):
    """A stable code suitable for translation, without remote response contents."""

    def __init__(self, code, *, cleanup_pending=False, api_code=None):
        super().__init__(code)
        self.code = code
        self.cleanup_pending = cleanup_pending
        self.api_code = api_code if type(api_code) is int and -(2**31) <= api_code < 2**31 else None


@dataclass(frozen=True)
class Session:
    token: str = field(repr=False)
    email: str = field(default="", repr=False)


@dataclass(frozen=True)
class UploadResult:
    file_id: int
    size: int
    cleanup_pending: bool = False


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CloudError("invalid_response")
    if not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
        raise CloudError("invalid_response")
    return int(value)


class HttpsTransport:
    """Verified TLS, no redirects, no cookies and no shared authentication headers."""

    def request(self, method, url, body, headers, timeout):
        try:
            parts = urlsplit(url)
            port = parts.port or 443
            if (parts.scheme != "https" or not parts.hostname or
                    parts.username is not None or parts.password is not None or
                    parts.fragment or port != 443):
                raise CloudError("invalid_upload_url")
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            connection = http.client.HTTPSConnection(
                parts.hostname, port=port, timeout=timeout,
                context=ssl.create_default_context())
            try:
                connection.request(method, target, body=body, headers=dict(headers))
                response = connection.getresponse()
                # Cloud error pages can contain request signatures. Never expose them.
                if not 200 <= response.status < 300:
                    raise CloudError("http_error")
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise CloudError("response_too_large")
                return payload
            finally:
                connection.close()
        except CloudError:
            raise
        except Exception:
            raise CloudError("connection_failed") from None


class _UploadReader:
    def __init__(self, stream, size, cancelled, progress):
        self.stream, self.size, self.remaining = stream, size, size
        self.cancelled, self.progress = cancelled, progress

    def read(self, count=65536):
        if count == 0:
            return b""
        if self.cancelled():
            raise CloudError("cancelled")
        if not self.remaining:
            return b""
        chunk = self.stream.read(min(self.remaining, 65536 if count < 0 else count))
        if not chunk:
            raise CloudError("input_changed")
        self.remaining -= len(chunk)
        self.progress("upload", (self.size - self.remaining) / self.size)
        return chunk


class CloudClient:
    def __init__(self, *, session=None, transport=None):
        self.session = session
        self._transport = transport if transport is not None else HttpsTransport()

    def __repr__(self):
        return f"CloudClient(signed_in={self.session is not None})"

    def _api(self, path, payload, *, authenticated=True, method="POST", timeout=60):
        if authenticated and (self.session is None or not self.session.token):
            raise CloudError("sign_in_required")
        timestamp, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
        signature_input = protocol.KEY_A + timestamp + protocol.XX_VERSION + protocol.KEY_B + nonce + protocol.KEY_A
        headers = {
            "Accept": "application/json", "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=UTF-8",
            "XX-Device-Type": protocol.DEVICE_TYPE, "XX-IS-CN": protocol.IS_CN,
            "XX-LANGUAGE": protocol.LANGUAGE, "XX-Nonce": nonce,
            "XX-Signature": hashlib.md5(signature_input.encode()).hexdigest().upper(),
            "XX-Timestamp": timestamp, "XX-Version": protocol.XX_VERSION,
        }
        if authenticated:
            headers["XX-Token"] = self.session.token
        try:
            raw = self._transport.request(method, protocol.CLOUD_BASE + protocol.GATEWAY_PREFIX + path,
                json.dumps(payload, separators=(",", ":")).encode() if payload is not None else None,
                headers, timeout)
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get("code")) is not int:
                raise CloudError("invalid_response")
            if result["code"] != 1:
                raise CloudError("api_rejected", api_code=result["code"])
            return result.get("data")
        except CloudError:
            raise
        except Exception:
            raise CloudError("invalid_response") from None

    def exchange_session(self, access_token, client_id):
        # The native account UI supplies this in memory; no manual token field.
        data = self._api(protocol.LOGIN_PATH,
            {"device_type": protocol.DEVICE_TYPE, "access_token": access_token, "pc_id": client_id},
            authenticated=False)
        if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
            raise CloudError("invalid_response")
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        self.session = Session(data["token"], user.get("user_email", ""))
        return self.session

    def list_printers(self):
        data = self._api(protocol.PRINTERS_PATH, {
            "page": 1, "limit": 1000, "is_farm": 0, "query_status": 0,
            "printer_id": 0, "machine_type": 0, "bulk_id": 0, "query_project": 0,
            "sort_by": 0, "type": "FDM", "label_name": None, "printer_ids": [],
            "ams_info": {"use_ams": False, "padding2": 0, "padding1": 0, "padding": 0, "ams_box_mapping": []},
        })
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise CloudError("invalid_response")
        return data

    def printer_info(self, printer_id):
        # Read-only endpoint; IDs are validated before becoming part of a URL.
        if (type(printer_id) not in (str, int) or len(str(printer_id)) > 19 or not str(printer_id).isascii() or
                not str(printer_id).isdigit() or not 0 < int(printer_id) < 2**63):
            raise CloudError("invalid_request")
        data = self._api("/v2/printer/info?id=" + str(int(printer_id)), None,
                         method="GET", timeout=15)
        if not isinstance(data, dict) or str(data.get("id")) != str(int(printer_id)):
            raise CloudError("invalid_response")
        return data

    def recent_projects(self):
        data = self._api("/work/project/getProjects?page=1&limit=100", None,
                         method="GET", timeout=15)
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise CloudError("invalid_response")
        return data

    def cloud_files(self, page):
        # This POST lists metadata; it does not claim, delete or start files.
        if type(page) is not int or not 1 <= page <= 10000:
            raise CloudError('invalid_request')
        data = self._api('/work/index/files', {'page':page, 'limit':20}, timeout=15)
        if data is None:
            return []
        if not isinstance(data, list) or len(data) > 20 or not all(isinstance(item, dict) for item in data):
            raise CloudError('invalid_response')
        return data

    def upload_gcode(self, path, *, cancelled=lambda: False, progress=None):
        """Upload prepared G-code only, with reservation cleanup on every exit."""
        def notify(phase, fraction):
            if progress is not None:
                try:
                    progress(phase, fraction)
                except Exception:
                    pass

        if cancelled():
            raise CloudError("cancelled")
        selected = Path(path)
        if selected.suffix.lower() != ".gcode":
            raise CloudError("gcode_required")
        try:
            stream = selected.open("rb")
        except OSError:
            raise CloudError("input_unreadable") from None
        with stream:
            size = os.fstat(stream.fileno()).st_size
            if size <= 0:
                raise CloudError("input_empty")
            notify("reserve", 0)
            reservation = self._api(protocol.UPLOAD_LOCK_PATH, {"size": size, "name": selected.name, "is_temp_file": 0})
            if not isinstance(reservation, dict):
                raise CloudError("invalid_response", cleanup_pending=True)
            try:
                reservation_id = _positive_id(reservation.get("id"))
            except CloudError:
                raise CloudError("invalid_response", cleanup_pending=True) from None
            file_id, failure, cleanup_pending = None, None, False
            registration_started, registration_rejected = False, False
            try:
                url = reservation.get("preSignUrl") or reservation.get("url")
                if not isinstance(url, str):
                    raise CloudError("invalid_upload_url")
                reader = _UploadReader(stream, size, cancelled, notify)
                self._transport.request("PUT", url, reader,
                    {"Content-Type": "application/octet-stream", "Content-Length": str(size)}, 120)
                if reader.remaining:
                    raise CloudError("incomplete_upload")
                if cancelled():
                    raise CloudError("cancelled")
                notify("register", 1)
                registration_started = True
                try:
                    registered = self._api(protocol.UPLOAD_CLAIM_PATH, {"user_lock_space_id": reservation_id})
                except CloudError as exc:
                    registration_rejected = exc.code == "api_rejected"
                    raise
                file_id = _positive_id(registered.get("id") if isinstance(registered, dict) else None)
            except CloudError as exc:
                failure = exc
            except Exception:
                failure = CloudError("upload_failed")
            finally:
                # A lost response can follow a successful registration. Do not
                # delete the uploaded object when its registration is uncertain.
                registration_uncertain = registration_started and file_id is None and not registration_rejected
                cleanup_pending = registration_uncertain
                try:
                    self._api(protocol.UPLOAD_UNLOCK_PATH,
                        {"id": reservation_id, "is_delete_cos": 0 if file_id is not None or registration_uncertain else 1})
                except Exception:
                    cleanup_pending = True
            if failure is not None:
                failure.cleanup_pending = cleanup_pending
                raise failure from None
            notify("done", 1)
            return UploadResult(file_id, size, cleanup_pending)
