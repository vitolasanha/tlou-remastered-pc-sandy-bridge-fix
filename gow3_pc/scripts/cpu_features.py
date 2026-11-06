"""Which of the PS4 (Jaguar) instruction sets this PC's CPU lacks, in pure Python.

The game's code uses BMI1, LZCNT, MOVBE and F16C on top of AVX / SSE4.2 / POPCNT. The first four
can be emulated in software (isa_emulation.py, placed by red_zone.py); the last three cannot.

  missing_emulatable()  names of the emulatable sets the CPU lacks, or None when it cannot be told
  missing_blocking()    names of the sets without which the game cannot run at all
  kinds_for(names)      isa_emulation Site kinds to replace for those sets

On Windows CPUID runs through a few bytes of machine code; elsewhere /proc/cpuinfo is read.
TLOU_FAKE_MISSING=BMI1,F16C pretends the CPU lacks those sets (tests).
"""
import os
import sys

FEATURE_KINDS = {
    'BMI1': ('tzcnt', 'andn', 'bextr', 'blsr', 'blsmsk', 'blsi'),
    'LZCNT': ('lzcnt',),
    'MOVBE': ('movbe_load', 'movbe_store'),
    'F16C': ('cvtph2ps', 'cvtps2ph'),
}
BLOCKING = ('SSE4.2', 'POPCNT', 'AVX')

# push rbx; mov eax,ecx; mov ecx,edx; cpuid; mov [r8],eax; mov [r8+4],ebx; mov [r8+8],ecx; mov [r8+12],edx; pop rbx; ret
# (Windows x64: ecx = leaf, edx = subleaf, r8 = uint32[4] output)
CPUID_CODE = bytes.fromhex('5389c889d10fa2' '458900' '41895804' '41894808' '4189500c' '5bc3')


def _cpuid_windows():
    import ctypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong]
    memory = k32.VirtualAlloc(None, 4096, 0x3000, 0x40)         # commit + reserve, execute/read/write
    if not memory:
        raise OSError('VirtualAlloc failed')
    ctypes.memmove(memory, CPUID_CODE, len(CPUID_CODE))
    function = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)(memory)

    def cpuid(leaf, sub=0):
        out = (ctypes.c_uint32 * 4)()
        function(leaf, sub, ctypes.addressof(out))
        return tuple(out)
    return cpuid


def _present_cpuid(cpuid):
    """Set of feature names present, from CPUID."""
    present = set()
    top = cpuid(0)[0]
    _, _, c1, _ = cpuid(1) if top >= 1 else (0, 0, 0, 0)
    for name, bit in (('SSE4.2', 20), ('MOVBE', 22), ('POPCNT', 23), ('AVX', 28), ('F16C', 29)):
        if c1 >> bit & 1:
            present.add(name)
    if top >= 7 and cpuid(7, 0)[1] >> 3 & 1:
        present.add('BMI1')
    if cpuid(0x80000000)[0] >= 0x80000001 and cpuid(0x80000001)[2] >> 5 & 1:
        present.add('LZCNT')
    return present


def _present_cpuinfo():
    flags = set()
    with open('/proc/cpuinfo') as f:
        for line in f:
            if line.startswith('flags'):
                flags = set(line.split(':', 1)[1].split())
                break
    names = {'sse4_2': 'SSE4.2', 'popcnt': 'POPCNT', 'avx': 'AVX', 'f16c': 'F16C', 'bmi1': 'BMI1',
             'movbe': 'MOVBE', 'abm': 'LZCNT', 'lzcnt': 'LZCNT'}
    return {names[x] for x in flags if x in names}


def present():
    """Set of the sets this CPU has, or None when that cannot be found out."""
    try:
        if sys.platform == 'win32':
            return _present_cpuid(_cpuid_windows())
        return _present_cpuinfo()
    except Exception:                  # no CPUID, no /proc: the CPU cannot be identified
        return None


def missing_emulatable():
    fake = os.environ.get('TLOU_FAKE_MISSING')
    if fake is not None:
        return [n for n in FEATURE_KINDS if n in {x.strip().upper() for x in fake.split(',')}]
    have = present()
    if have is None:
        return None
    return [n for n in FEATURE_KINDS if n not in have]


def missing_blocking():
    have = present()
    if have is None:
        return []
    return [n for n in BLOCKING if n not in have]


def kinds_for(names):
    return frozenset(k for n in names for k in FEATURE_KINDS[n])


if __name__ == '__main__':
    have = present()
    if have is None:
        print('Could not identify the CPU (set TLOU_ISA_REWRITE=force to emulate the instructions anyway).')
    else:
        print('CPU has:    ', ', '.join(sorted(have)) or '-')
        print('Emulated:   ', ', '.join(missing_emulatable()) or 'nothing (the CPU has them all)')
        print('Blocking:   ', ', '.join(missing_blocking()) or 'nothing')
