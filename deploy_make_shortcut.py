"""Create the SparkBuddy desktop shortcut via IShellLink COM (ctypes).

Run with: spark-buddy/.venv/Scripts/python.exe deploy_make_shortcut.py
"""
import ctypes
import os
from ctypes import POINTER, c_int, c_void_p, c_wchar_p, cast, byref
from ctypes.wintypes import DWORD, WORD, BOOL

HRESULT = ctypes.HRESULT


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", ctypes.c_ubyte * 8)]


def make_guid(s):
    s = s.strip("{}")
    p1, p2, p3, p4, p5 = s.split("-")
    g = GUID()
    g.Data1, g.Data2, g.Data3 = int(p1, 16), int(p2, 16), int(p3, 16)
    raw = bytes.fromhex(p4) + bytes.fromhex(p5)
    for i, b in enumerate(raw):
        g.Data4[i] = b
    return g


CLSID_ShellLink = make_guid("00021401-0000-0000-C000-000000000046")
IID_IShellLinkW = make_guid("000214F9-0000-0000-C000-000000000046")
IID_IPersistFile = make_guid("0000010B-0000-0000-C000-000000000046")
CLSCTX_INPROC_SERVER = 1

# IShellLinkW vtable: IUnknown=0..2, GetPath=3, ..., SetDescription=7,
# SetWorkingDirectory=9, SetIconLocation=17, SetPath=20
SL_SETDESCRIPTION = 7
SL_SETWORKDIR = 9
SL_SETICONLOCATION = 17
SL_SETPATH = 20
# IPersistFile vtable: Save=6
PF_SAVE = 6

SetPathProto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)
SetWorkDirProto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)
SetIconLocProto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)
SetDescProto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)
SaveProto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, BOOL)
QueryInterfaceProto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))
ReleaseProto = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)


def get_vtbl(punk):
    vt_ptr = cast(punk, POINTER(c_void_p)).contents
    return cast(vt_ptr, POINTER(c_void_p * 32)).contents


def vtbl_call(punk, index, proto, *args):
    fn = proto(get_vtbl(punk)[index])
    return fn(punk, *args)


def make_shortcut(lnk_path, target, workdir, icon, description):
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    psl = c_void_p()
    hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER, byref(IID_IShellLinkW), byref(psl))
    if hr:
        raise OSError("CoCreateInstance: 0x%08x" % (hr & 0xFFFFFFFF))
    try:
        for idx, proto, args, tag in [
            (SL_SETPATH, SetPathProto, (target,), "SetPath"),
            (SL_SETWORKDIR, SetWorkDirProto, (workdir,), "SetWorkingDirectory"),
            (SL_SETICONLOCATION, SetIconLocProto, (icon, 0), "SetIconLocation"),
            (SL_SETDESCRIPTION, SetDescProto, (description,), "SetDescription"),
        ]:
            hr = vtbl_call(psl, idx, proto, *args)
            if hr:
                raise OSError("%s: 0x%08x" % (tag, hr & 0xFFFFFFFF))
        ppf = c_void_p()
        hr = QueryInterfaceProto(get_vtbl(psl)[0])(psl, byref(IID_IPersistFile), byref(ppf))
        if hr:
            raise OSError("QueryInterface: 0x%08x" % (hr & 0xFFFFFFFF))
        try:
            hr = vtbl_call(ppf, PF_SAVE, SaveProto, lnk_path, 0)
            if hr:
                raise OSError("Save: 0x%08x" % (hr & 0xFFFFFFFF))
        finally:
            ReleaseProto(get_vtbl(ppf)[2])(ppf)
    finally:
        ReleaseProto(get_vtbl(psl)[2])(psl)
        ole32.CoUninitialize()


if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf)  # CSIDL_DESKTOP
    desktop = buf.value
    lnk = os.path.join(desktop, "火花管家.lnk")
    make_shortcut(
        lnk,
        target=os.path.join(root, "start.bat"),
        workdir=root,
        icon=os.path.join(root, "icon.ico"),
        description="SparkBuddy - 抖音火花自动维护面板 (127.0.0.1:8765)",
    )
    print("saved:", os.path.exists(lnk), "->", lnk)
    print("size:", os.path.getsize(lnk), "bytes")
