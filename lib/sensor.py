import ctypes, numpy as np

class FTRSCAN_IMAGE_SIZE(ctypes.Structure):
    _fields_ = [("nWidth",  ctypes.c_int),
                ("nHeight", ctypes.c_int),
                ("nImageSize", ctypes.c_int)]

LIB = ctypes.CDLL("/home/sku/fingerprint/lib/libScanAPI.so")

# === 64-bit-saubere Signaturen (FIX gegen Segfault in Threads) ===
LIB.ftrScanOpenDevice.restype  = ctypes.c_void_p
LIB.ftrScanOpenDevice.argtypes = []

LIB.ftrScanCloseDevice.restype  = None
LIB.ftrScanCloseDevice.argtypes = [ctypes.c_void_p]

LIB.ftrScanGetImageSize.restype  = ctypes.c_int
LIB.ftrScanGetImageSize.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(FTRSCAN_IMAGE_SIZE)]

LIB.ftrScanGetFrame.restype  = ctypes.c_int
LIB.ftrScanGetFrame.argtypes = [ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_ubyte),
                                ctypes.c_void_p]


def open_scanner():
    h = LIB.ftrScanOpenDevice()
    if not h:
        raise RuntimeError("Scanner nicht gefunden!")
    return h

def get_size(handle):
    s = FTRSCAN_IMAGE_SIZE()
    LIB.ftrScanGetImageSize(ctypes.c_void_p(handle), ctypes.byref(s))
    return s.nWidth, s.nHeight, s.nImageSize

def capture(handle, w, h, size):
    buf = (ctypes.c_ubyte * size)()
    if not LIB.ftrScanGetFrame(ctypes.c_void_p(handle), buf, None):
        return None
    return np.frombuffer(buf, dtype=np.uint8).reshape((h, w))

def close_scanner(handle):
    LIB.ftrScanCloseDevice(ctypes.c_void_p(handle))

def is_finger_on(img):
    return img.std() > 15
