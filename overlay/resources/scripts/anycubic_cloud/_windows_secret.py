import os

def _protect(payload, decrypt=False):
    """Windows user-scoped DPAPI. Never fall back to writing plaintext."""
    if os.name != "nt":
        raise RuntimeError("Protected session storage requires Windows.")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.c_void_p))
    result = Blob()
    pointer = ctypes.POINTER(Blob)
    if decrypt:
        operation = crypt.CryptUnprotectData
        operation.argtypes = [pointer, ctypes.c_void_p, pointer, ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, pointer]
        second = None
    else:
        operation = crypt.CryptProtectData
        operation.argtypes = [pointer, wintypes.LPCWSTR, pointer, ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, pointer]
        second = "OrcaCubic Anycubic Cloud session"
    operation.restype = wintypes.BOOL
    try:
        if not operation(ctypes.byref(source), second, None, None, None, 1, ctypes.byref(result)):
            raise RuntimeError("Windows session protection failed.")
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)
