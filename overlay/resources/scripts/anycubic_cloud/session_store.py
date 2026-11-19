"""User-scoped encrypted account persistence for the Windows native provider.

The host supplies its dedicated config directory. No plugin or vendor account
files are discovered or imported implicitly, and no plaintext fallback exists.
"""

import base64
import json
import os
from pathlib import Path
import tempfile

from ._windows_secret import _protect
from .client import CloudError, Session


class SessionStore:
    def __init__(self, path):
        self._path = Path(path)

    @staticmethod
    def _session(data):
        if (not isinstance(data, dict) or not isinstance(data.get("token"), str) or
                not data["token"] or not isinstance(data.get("email"), str)):
            raise CloudError("invalid_session")
        return Session(data["token"], data["email"])

    def save(self, session):
        temporary = None
        try:
            validated = self._session({"token": session.token, "email": session.email})
            raw = json.dumps({"token": validated.token, "email": validated.email},
                             ensure_ascii=False).encode("utf-8")
            if len(raw) > 65536:
                raise CloudError("session_too_large")
            # Protect before creating any file, including the temporary file.
            sealed = _protect(raw)
            payload = json.dumps({"schema": 1, "protection": "windows-user-dpapi",
                                  "sealed": base64.b64encode(sealed).decode("ascii")}).encode()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self._path.parent,
                    prefix=".anycubic-session-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except CloudError:
            raise
        except Exception:
            raise CloudError("secure_session_save_failed") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def load(self):
        try:
            with self._path.open("rb") as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                raise CloudError("secure_session_unreadable")
            envelope = json.loads(raw)
            if (not isinstance(envelope, dict) or type(envelope.get("schema")) is not int or envelope["schema"] != 1 or
                    envelope.get("protection") != "windows-user-dpapi" or
                    not isinstance(envelope.get("sealed"), str)):
                raise CloudError("secure_session_unreadable")
            sealed = base64.b64decode(envelope["sealed"], validate=True)
            return self._session(json.loads(_protect(sealed, decrypt=True)))
        except FileNotFoundError:
            return None
        except CloudError:
            raise
        except Exception:
            raise CloudError("secure_session_unreadable") from None

    def clear(self):
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            raise CloudError("secure_session_clear_failed") from None

    def import_existing(self, source):
        """Explicit GUI-selected import of the previous encrypted plugin format."""
        try:
            with Path(source).open("rb") as stream:
                raw = stream.read(262145)
            if len(raw) > 262144:
                raise CloudError("secure_session_unreadable")
            envelope = json.loads(raw)
            if (not isinstance(envelope, dict) or type(envelope.get("schema")) is not int or
                    envelope["schema"] != 2 or envelope.get("protection") != "windows-user-dpapi"):
                raise CloudError("secure_session_unreadable")
            sealed = base64.b64decode(envelope["sealed"], validate=True)
            data = json.loads(_protect(sealed, decrypt=True))
            if not isinstance(data, dict):
                raise CloudError("secure_session_unreadable")
            session = self._session({"token": data.get("workbench_token"), "email": data.get("email", "")})
            self.save(session)
            return session
        except CloudError:
            raise
        except Exception:
            raise CloudError("secure_session_unreadable") from None
