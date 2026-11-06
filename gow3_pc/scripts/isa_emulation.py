"""Software replacements for the PS4 (Jaguar) instructions that CPUs older than Haswell lack.

Sandy Bridge / Ivy Bridge (and some Xeon E5 v1/v2) have AVX but not BMI1 (andn, bextr, blsi, blsmsk,
blsr, tzcnt), LZCNT, MOVBE or, before Ivy Bridge, F16C (vcvtph2ps, vcvtps2ph). The game's code uses
all of them. tzcnt and lzcnt are the worst case: their encodings are `rep bsf` / `rep bsr`, so a CPU
without them does not fault, it silently computes something else.

parse(raw, ip) decodes one such instruction from its bytes (no disassembler needed) and
emulate(site, at) returns machine code, to be placed at image offset `at`, that does the same
thing: same results, same flags that the instruction defines, no other register or memory changed.
The code is self-contained (uses the stack 128 bytes below rsp or deeper, so the PS4 red zone and the
Windows exception frame stay intact), has no jumps out of itself and falls through at its end.
Only AVX1 / SSE2 / baseline x86-64 instructions are used.

Self test: gow3_pc/tests/test_isa_emulation.py runs every emulation against the real instruction.
"""
from dataclasses import dataclass
import struct

RSP = 4
RCX = 1
SCRATCH_POOL = (0, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 1)   # rax rdx rbx rsi rdi r8.. rcx last
RED_ZONE = 128


class Unsupported(Exception):
    pass


@dataclass
class Mem:
    base: object = None        # register number or None
    index: object = None
    scale: int = 1
    disp: int = 0
    rip: bool = False          # [rip + disp]; target is the absolute (image) address
    target: int = 0
    fixed: bool = False        # a stack-frame slot: relative to the current rsp, never shifted

    def regs(self):
        return {r for r in (self.base, self.index) if r is not None}


@dataclass
class Site:
    kind: str                  # tzcnt lzcnt movbe_load movbe_store andn bextr blsr blsmsk blsi cvtph2ps cvtps2ph
    size: int                  # operand size in bits (16, 32, 64); vector kinds: 128 or 256 (the vector length)
    length: int                # instruction length
    reg: int = 0               # ModRM.reg operand (register number)
    rm: object = None          # int (register) or Mem
    vvvv: int = 0
    imm: int = 0


# ---------------------------------------------------------------- decoding

def _modrm(raw, pos, rex_x, rex_b, ip, total_len_hint=None):
    """Decodes ModRM[/SIB/disp] at raw[pos:]. Returns (mod, reg_field, rm, new_pos); rm is a register
    number (mod == 3) or a Mem (rip target resolved by the caller once the length is known)."""
    modrm = raw[pos]
    pos += 1
    mod, reg, rm = modrm >> 6, (modrm >> 3) & 7, modrm & 7
    if mod == 3:
        return mod, reg, rm | (rex_b << 3), pos
    mem = Mem()
    if rm == 4:
        sib = raw[pos]
        pos += 1
        scale, index, base = 1 << (sib >> 6), (sib >> 3) & 7, sib & 7
        index |= rex_x << 3
        if index != 4:
            mem.index, mem.scale = index, scale
        if base == 5 and mod == 0:
            mem.base = None
            mem.disp, = struct.unpack_from('<i', raw, pos)
            pos += 4
        else:
            mem.base = base | (rex_b << 3)
    elif rm == 5 and mod == 0:
        mem.rip = True
        mem.disp, = struct.unpack_from('<i', raw, pos)
        pos += 4
    else:
        mem.base = rm | (rex_b << 3)
    if mod == 1:
        mem.disp, = struct.unpack_from('<b', raw, pos)
        pos += 1
    elif mod == 2:
        mem.disp, = struct.unpack_from('<i', raw, pos)
        pos += 4
    return mod, reg, mem, pos


def parse(raw, ip=0):
    """Site for the instruction at the start of raw, or None when it is not one of ours (or has a
    form that is not handled: segment or address-size prefixes, rsp as a register operand)."""
    try:
        site = _parse(bytes(raw[:16]), ip)
    except (IndexError, struct.error):
        return None
    if site is not None and isinstance(site.rm, Mem) and site.rm.rip:
        site.rm.target = ip + site.length + site.rm.disp
    return site


def _parse(raw, ip):
    pos = 0
    opsize16 = repz = False
    while pos < len(raw) and raw[pos] in (0x66, 0xf3, 0xf2, 0x67, 0x2e, 0x36, 0x3e, 0x26, 0x64, 0x65):
        if raw[pos] == 0x66:
            opsize16 = True
        elif raw[pos] == 0xf3:
            repz = True
        else:
            return None                      # f2, address size, segments
        pos += 1
    b = raw[pos]
    if b in (0xc4, 0xc5):
        if opsize16 or repz:
            return None
        return _parse_vex(raw, pos)
    rex = 0
    if 0x40 <= b <= 0x4f:
        rex = b
        pos += 1
    w, r, x, bb = (rex >> 3) & 1, (rex >> 2) & 1, (rex >> 1) & 1, rex & 1
    size = 64 if w else (16 if opsize16 else 32)
    if raw[pos] != 0x0f:
        return None
    pos += 1
    op = raw[pos]
    pos += 1
    if op in (0xbc, 0xbd) and repz:
        mod, reg, rm, pos = _modrm(raw, pos, x, bb, ip)
        return Site('tzcnt' if op == 0xbc else 'lzcnt', size, pos, reg | (r << 3), rm)
    if op == 0x38 and raw[pos] in (0xf0, 0xf1) and not repz:
        which = raw[pos]
        pos += 1
        mod, reg, rm, pos = _modrm(raw, pos, x, bb, ip)
        if mod == 3:
            return None
        return Site('movbe_load' if which == 0xf0 else 'movbe_store', size, pos, reg | (r << 3), rm)
    return None


def _parse_vex(raw, pos):
    if raw[pos] == 0xc5:
        return None                          # 2-byte VEX is map 0F: nothing of ours lives there
    b1, b2 = raw[pos + 1], raw[pos + 2]
    r, x, bb = (~b1 >> 7) & 1, (~b1 >> 6) & 1, (~b1 >> 5) & 1
    mmap = b1 & 0x1f
    w, vvvv, l, pp = b2 >> 7, (~b2 >> 3) & 15, (b2 >> 2) & 1, b2 & 3
    op = raw[pos + 3]
    pos += 4
    size = 64 if w else 32
    if mmap == 2 and pp == 0 and not l and op in (0xf2, 0xf3, 0xf7):
        mod, reg, rm, pos = _modrm(raw, pos, x, bb, 0)
        if op == 0xf2:
            kind = 'andn'
        elif op == 0xf7:
            kind = 'bextr'
        else:
            kind = {1: 'blsr', 2: 'blsmsk', 3: 'blsi'}.get(reg)
            if kind is None:
                return None
        site = Site(kind, size, pos, reg | (r << 3), rm, vvvv)
        if kind in ('blsr', 'blsmsk', 'blsi'):
            site.reg = vvvv                  # destination is vvvv; the source is r/m
        return site
    if mmap == 2 and pp == 1 and not w and op == 0x13:
        mod, reg, rm, pos = _modrm(raw, pos, x, bb, 0)
        return Site('cvtph2ps', 256 if l else 128, pos, reg | (r << 3), rm)
    if mmap == 3 and pp == 1 and not w and op == 0x1d:
        mod, reg, rm, pos = _modrm(raw, pos, x, bb, 0)
        imm = raw[pos]
        return Site('cvtps2ph', 256 if l else 128, pos + 1, reg | (r << 3), rm, imm=imm)
    return None


# ---------------------------------------------------------------- assembler

class Asm:
    """Appends instructions at image offset `at`; supports forward short jumps through labels."""

    def __init__(self, at):
        self.at = at
        self.code = bytearray()
        self.labels = {}
        self.fixups = []
        self.shift = 0                       # how far below its entry value rsp is (for [rsp + x] operands)

    @property
    def here(self):
        return self.at + len(self.code)

    # -- encoding helpers
    def _rm(self, regfield, rm):
        """(bytes of modrm/sib/disp, rex.x, rex.b, offset of a rip disp32 inside, target)."""
        if not isinstance(rm, Mem):
            return bytes([0xc0 | (regfield & 7) << 3 | (rm & 7)]), 0, (rm >> 3) & 1, None, 0
        m = rm
        reg3 = (regfield & 7) << 3
        if m.rip:
            return bytes([reg3 | 5]) + b'\0\0\0\0', 0, 0, 1, m.target
        disp = m.disp + (self.shift if m.base == RSP and not m.fixed else 0)
        if m.base is None:                   # [index*scale + disp32] (or absolute)
            index = m.index if m.index is not None else 4
            ss = {1: 0, 2: 1, 4: 2, 8: 3}[m.scale if m.index is not None else 1]
            return (bytes([reg3 | 4, ss << 6 | (index & 7) << 3 | 5]) + struct.pack('<i', disp),
                    (index >> 3) & 1 if m.index is not None else 0, 0, None, 0)
        base = m.base
        need_sib = m.index is not None or (base & 7) == 4
        if disp == 0 and (base & 7) != 5:
            mod, tail = 0, b''
        elif -128 <= disp <= 127:
            mod, tail = 1, struct.pack('<b', disp)
        else:
            mod, tail = 2, struct.pack('<i', disp)
        if need_sib:
            index = m.index if m.index is not None else 4
            ss = {1: 0, 2: 1, 4: 2, 8: 3}[m.scale if m.index is not None else 1]
            head = bytes([mod << 6 | reg3 | 4, ss << 6 | (index & 7) << 3 | (base & 7)])
            x = (index >> 3) & 1 if m.index is not None else 0
        else:
            head = bytes([mod << 6 | reg3 | (base & 7)])
            x = 0
        return head + tail, x, (base >> 3) & 1, None, 0

    def _emit(self, pre, rexw, regfield, rm, opcode, imm=b'', force_rex=False, vex=None):
        body, x, b, riprel, target = self._rm(regfield, rm)
        if vex is not None:
            vmap, pp, l, w, vvvv = vex
            head = bytes([0xc4, (~(regfield >> 3) & 1) << 7 | (~x & 1) << 6 | (~b & 1) << 5 | vmap,
                          w << 7 | (~vvvv & 15) << 3 | l << 2 | pp]) + opcode
        else:
            rexb = 0x40 | rexw << 3 | ((regfield >> 3) & 1) << 2 | x << 1 | b
            head = pre + (bytes([rexb]) if (rexb != 0x40 or force_rex) else b'') + opcode
        data = bytearray(head + body + imm)
        if riprel is not None:
            at = len(head) + riprel
            disp = target - (self.here + len(data))
            struct.pack_into('<i', data, at, disp)
        self.code += data

    def raw(self, data):
        self.code += data

    # -- general purpose instructions (size 16/32/64)
    def _gp(self, size, regfield, rm, opcode, imm=b''):
        pre = b'\x66' if size == 16 else b''
        self._emit(pre, 1 if size == 64 else 0, regfield, rm, opcode, imm)

    def mov_load(self, size, reg, rm):
        self._gp(size, reg, rm, b'\x8b')

    def mov_store(self, size, rm, reg):
        self._gp(size, reg, rm, b'\x89')

    def mov_rr(self, size, dst, src):
        self._gp(size, dst, src, b'\x8b')

    def mov_ri(self, size, dst, imm):
        if size == 16:
            self.raw(b'\x66' + (bytes([0x41]) if dst >= 8 else b'') + bytes([0xb8 + (dst & 7)]) + struct.pack('<H', imm))
        else:
            self.raw((bytes([0x41]) if dst >= 8 else b'') + bytes([0xb8 + (dst & 7)]) + struct.pack('<I', imm))

    def lea(self, size, dst, mem):
        self._gp(size, dst, mem, b'\x8d')

    def alu_rr(self, op, size, dst, src):    # op: 0x23 and, 0x33 xor, 0x0b or
        self._gp(size, dst, src, bytes([op]))

    def alu_rm(self, op, size, dst, mem):
        self._gp(size, dst, mem, bytes([op]))

    def test_rr(self, size, a, b):
        self._gp(size, b, a, b'\x85')

    def unary(self, ext, size, reg):         # 2 not, 3 neg
        self._gp(size, ext, reg, b'\xf7')

    def xor_ri8(self, size, dst, imm):
        self._gp(size, 6, dst, b'\x83', bytes([imm]))

    def shift_cl(self, ext, size, reg):      # 4 shl, 5 shr
        self._gp(size, ext, reg, b'\xd3')

    def shr_ri8(self, size, reg, imm):
        self._gp(size, 5, reg, b'\xc1', bytes([imm]))

    def rol_ri8(self, size, reg, imm):
        self._gp(size, 0, reg, b'\xc1', bytes([imm]))

    def bsf(self, size, dst, rm):
        self._gp(size, dst, rm, b'\x0f\xbc')

    def bsr(self, size, dst, rm):
        self._gp(size, dst, rm, b'\x0f\xbd')

    def cmp_cl_imm8(self, imm):
        self.raw(b'\x80\xf9' + bytes([imm]))

    def bswap(self, size, reg):
        self.raw((bytes([0x48 | (reg >> 3)]) if size == 64 else (bytes([0x41]) if reg >= 8 else b''))
                 + bytes([0x0f, 0xc8 + (reg & 7)]))

    def stc(self):
        self.raw(b'\xf9')

    def pushfq(self):
        self.raw(b'\x9c')
        self.shift += 8

    def popfq(self):
        self.raw(b'\x9d')
        self.shift -= 8

    def frame_in(self, n):
        self.raw(b'\x48\x8d\xa4\x24' + struct.pack('<i', -n))
        self.shift += n

    def frame_out(self, n):
        self.raw(b'\x48\x8d\xa4\x24' + struct.pack('<i', n))
        self.shift -= n

    # -- jumps
    def jcc(self, cc, label):                # cc: opcode low nibble (4 jz 5 jnz 3 jnc 2 jc)
        self.raw(bytes([0x70 | cc, 0]))
        self.fixups.append((len(self.code) - 1, label))

    def jmp(self, label):
        self.raw(b'\xeb\0')
        self.fixups.append((len(self.code) - 1, label))

    def label(self, name):
        self.labels[name] = len(self.code)

    def finish(self):
        for pos, label in self.fixups:
            rel = self.labels[label] - (pos + 1)
            if not -128 <= rel <= 127:
                raise Unsupported('short jump out of range')
            self.code[pos] = rel & 0xff
        return bytes(self.code)

    # -- AVX (VEX.128 unless l=1)
    def v(self, vmap, pp, op, reg, rm, vvvv=0, l=0, w=0, imm=b''):
        self._emit(b'', 0, reg, rm, bytes([op]), imm, vex=(vmap, pp, l, w, vvvv))


def _frame_slot(off):
    """A slot of the current frame, [rsp + off] with rsp already lowered (not shifted again)."""
    return Mem(base=RSP, disp=off, fixed=True)


def _pick(exclude, count=1):
    free = [r for r in SCRATCH_POOL if r not in exclude]
    if len(free) < count:
        raise Unsupported('no scratch register')
    return free[:count]


def _regs_of(rm):
    return rm.regs() if isinstance(rm, Mem) else {rm}


# ---------------------------------------------------------------- emulations

def emulate(site, at):
    """Machine code for `site` to be placed at image offset `at` (see the module doc)."""
    asm = Asm(at)
    if not isinstance(site.rm, Mem) and site.rm == RSP and site.kind not in ('cvtph2ps', 'cvtps2ph'):
        raise Unsupported('rsp operand')
    if site.kind in ('tzcnt', 'lzcnt'):
        _bitscan(asm, site)
    elif site.kind == 'movbe_load':
        _movbe_load(asm, site)
    elif site.kind == 'movbe_store':
        _movbe_store(asm, site)
    elif site.kind in ('andn', 'blsr', 'blsmsk', 'blsi', 'bextr'):
        _bmi(asm, site)
    elif site.kind == 'cvtph2ps':
        _cvtph2ps(asm, site)
    elif site.kind == 'cvtps2ph':
        _cvtps2ph(asm, site)
    else:
        raise Unsupported(site.kind)
    return asm.finish()


def _bitscan(a, s):
    n = s.size
    a.frame_in(RED_ZONE)
    (a.bsf if s.kind == 'tzcnt' else a.bsr)(n, s.reg, s.rm)
    a.jcc(4, 'zero')                                   # jz
    if s.kind == 'tzcnt':
        a.test_rr(n, s.reg, s.reg)
    else:
        a.xor_ri8(n, s.reg, n - 1)
    a.jmp('done')
    a.label('zero')
    a.mov_ri(n, s.reg, n)
    a.test_rr(n, s.reg, s.reg)
    a.stc()
    a.label('done')
    a.frame_out(RED_ZONE)


def _movbe_load(a, s):
    n = s.size
    a.frame_in(RED_ZONE)
    a.mov_load(n, s.reg, s.rm)
    if n == 16:
        a.pushfq()
        a.rol_ri8(16, s.reg, 8)
        a.popfq()
    else:
        a.bswap(n, s.reg)
    a.frame_out(RED_ZONE)


def _movbe_store(a, s):
    n = s.size
    if n == 64 and s.reg not in s.rm.regs() and s.reg != RSP:   # bswap r32 would clear the upper half
        a.frame_in(RED_ZONE)
        a.bswap(n, s.reg)
        a.mov_store(n, s.rm, s.reg)
        a.bswap(n, s.reg)
        a.frame_out(RED_ZONE)
        return
    if s.reg == RSP:
        raise Unsupported('rsp operand')
    (t,) = _pick(s.rm.regs() | {s.reg})
    f = RED_ZONE + 8
    a.frame_in(f)
    a.mov_store(64, _frame_slot(0), t)
    if n == 16:
        a.mov_rr(32, t, s.reg)
        a.pushfq()
        a.rol_ri8(16, t, 8)
        a.popfq()
    else:
        a.mov_rr(n, t, s.reg)
        a.bswap(n, t)
    a.mov_store(n, s.rm, t)
    a.mov_load(64, t, _frame_slot(0))
    a.frame_out(f)


def _bmi(a, s):
    n = s.size
    F = RED_ZONE + 32
    rm_regs = _regs_of(s.rm)
    if s.kind == 'andn':
        a_reg, b_rm = s.vvvv, s.rm
        used = rm_regs | {a_reg}
        if a_reg == RSP or (not isinstance(b_rm, Mem) and b_rm == RSP):
            raise Unsupported('rsp operand')
        (S,) = _pick(used)
        a.frame_in(F)
        a.mov_store(64, _frame_slot(0), S)
        a.mov_rr(n, S, a_reg)
        a.unary(2, n, S)
        if isinstance(b_rm, Mem):
            a.alu_rm(0x23, n, S, b_rm)
        else:
            a.alu_rr(0x23, n, S, b_rm)
        a.mov_store(64, _frame_slot(24), S)
        a.mov_load(64, S, _frame_slot(0))
        a.mov_load(n, s.reg, _frame_slot(24))
        a.frame_out(F)
        return
    if s.kind == 'bextr':
        ctrl = s.vvvv
        used = rm_regs | {ctrl, RCX}
        if ctrl == RSP or (not isinstance(s.rm, Mem) and s.rm == RSP):
            raise Unsupported('rsp operand')
        S, T = _pick(used, 2)
        a.frame_in(F)
        a.mov_store(64, _frame_slot(0), S)
        a.mov_store(64, _frame_slot(8), T)
        a.mov_store(64, _frame_slot(16), RCX)
        a.mov_load(n, S, s.rm)
        a.mov_rr(n, T, ctrl)
        a.mov_rr(32, RCX, T)
        a.cmp_cl_imm8(n)
        a.jcc(3, 'zero')                             # jae zero
        a.shift_cl(5, n, S)
        a.shr_ri8(n, T, 8)
        a.mov_rr(32, RCX, T)
        a.cmp_cl_imm8(n)
        a.jcc(3, 'nomask')                           # jae nomask
        a.mov_ri(32, T, 1)
        a.shift_cl(4, n, T)
        a.lea(n, T, Mem(base=T, disp=-1))
        a.alu_rr(0x23, n, S, T)
        a.label('nomask')
        a.test_rr(n, S, S)
        a.jmp('done')
        a.label('zero')
        a.alu_rr(0x33, n, S, S)
        a.label('done')
        a.mov_store(64, _frame_slot(24), S)
        a.mov_load(64, S, _frame_slot(0))
        a.mov_load(64, T, _frame_slot(8))
        a.mov_load(64, RCX, _frame_slot(16))
        a.mov_load(n, s.reg, _frame_slot(24))
        a.frame_out(F)
        return
    # blsr / blsmsk / blsi: destination vvvv (stored in s.reg), source r/m
    if not isinstance(s.rm, Mem) and s.rm == RSP:
        raise Unsupported('rsp operand')
    S, T = _pick(rm_regs, 2)
    a.frame_in(F)
    a.mov_store(64, _frame_slot(0), S)
    a.mov_store(64, _frame_slot(8), T)
    a.mov_load(n, S, s.rm)
    if s.kind == 'blsi':
        a.mov_rr(n, T, S)
        a.unary(3, n, T)                              # neg: CF = (src != 0)
        a.jcc(3, 'zero')                              # jnc
        a.alu_rr(0x23, n, T, S)
        a.stc()
        a.jmp('done')
        a.label('zero')
        a.alu_rr(0x23, n, T, S)
    else:
        a.lea(n, T, Mem(base=S, disp=-1))
        a.test_rr(n, S, S)
        a.jcc(4, 'zero')                              # jz
        a.alu_rr(0x23 if s.kind == 'blsr' else 0x33, n, T, S)
        a.jmp('done')
        a.label('zero')
        a.alu_rr(0x33, n, T, T if s.kind == 'blsr' else S)
        a.stc()
    a.label('done')
    a.mov_store(64, _frame_slot(24), T)
    a.mov_load(64, S, _frame_slot(0))
    a.mov_load(64, T, _frame_slot(8))
    a.mov_load(n, s.reg, _frame_slot(24))
    a.frame_out(F)


# ---- F16C ---------------------------------------------------------------

def _splat(a, gpr, x, value):
    a.mov_ri(32, gpr, value & 0xffffffff)
    a.v(1, 1, 0x6e, x, gpr)                           # vmovd x, gpr
    a.v(1, 1, 0x70, x, x, imm=b'\0')                  # vpshufd x, x, 0


def _vop(a, op, dst, s1, s2, pp=1):                   # vpand etc: dst = s1 op s2
    a.v(1, pp, op, dst, s2, vvvv=s1)


def _vshift(a, ext, dst, src, imm):                   # vpslld/vpsrld/vpsrad dst, src, imm
    a.v(1, 1, 0x72, ext, src, vvvv=dst, imm=bytes([imm]))


def _h2f(a, g, inp, out, tmp):
    """out = 4 floats converted from the 4 halves in the low 64 bits of inp (clobbers inp, tmp)."""
    A, B, C, E = inp, tmp[0], tmp[1], tmp[2]
    _vop(a, 0xef, out, out, out)                      # out = 0
    _vop(a, 0x61, A, A, out)                          # vpunpcklwd: halves -> 32-bit lanes
    _splat(a, g, C, 0x7fff)
    _vop(a, 0xdb, B, A, C)                            # B = expmant
    _splat(a, g, C, 0x8000)
    _vop(a, 0xdb, A, A, C)
    _vshift(a, 6, A, A, 16)                           # A = sign << 16
    _vshift(a, 6, out, B, 13)                         # shifted = expmant << 13
    _splat(a, g, C, 0x77800000)
    a.v(1, 0, 0x59, out, C, vvvv=out)                 # vmulps out, out, 2^112
    _splat(a, g, E, 0x7bff)
    _vop(a, 0x66, C, B, E)                            # C = expmant > 0x7bff (inf/nan)
    _splat(a, g, E, 0x7f800000)
    _vop(a, 0xdb, C, C, E)
    _vop(a, 0xeb, out, out, C)
    _splat(a, g, E, 0x7c00)
    _vop(a, 0x66, C, B, E)                            # C = expmant > 0x7c00 (nan)
    _splat(a, g, E, 0x400000)
    _vop(a, 0xdb, C, C, E)                            # quiet bit
    _vop(a, 0xeb, out, out, C)
    _vop(a, 0xeb, out, out, A)                        # sign


def _pick_xmm(exclude, count):
    free = [r for r in range(16) if r not in exclude]
    return free[:count]


def _cvtph2ps(a, s):
    wide = s.size == 256
    dst = s.reg
    src_reg = None if isinstance(s.rm, Mem) else s.rm
    exclude = {dst} | ({src_reg} if src_reg is not None else set())
    count = 6 if wide else 5
    xs = _pick_xmm(exclude, count)
    A, B, C, E, D1 = xs[:5]
    D2 = xs[5] if wide else None
    (g,) = _pick(s.rm.regs() if isinstance(s.rm, Mem) else set())
    F = RED_ZONE + 32 * count + 8
    a.frame_in(F)
    for i, x in enumerate(xs):
        a.v(1, 0, 0x11, x, _frame_slot(32 * i), l=1)  # vmovups [slot], ymm: the guest's values stay
    a.mov_store(64, _frame_slot(32 * count), g)
    if src_reg is None:
        if wide:
            a.v(1, 2, 0x6f, A, s.rm)                  # vmovdqu xmm, m128
        else:
            a.v(1, 2, 0x7e, A, s.rm)                  # vmovq xmm, m64
    else:
        a.v(1, 1, 0x6f, A, src_reg)                   # vmovdqa xmm, xmm
    if wide:
        a.v(1, 1, 0x73, 3, A, vvvv=D2, imm=b'\x08')   # vpsrldq D2, A, 8: the high four halves
    _h2f(a, g, A, D1, (B, C, E))                      # D1 = low four floats
    if wide:
        _h2f(a, g, D2, A, (B, C, E))                  # A = high four floats
        a.v(3, 1, 0x18, D1, A, vvvv=D1, l=1, imm=b'\x01')   # vinsertf128 D1, D1, A, 1
        a.v(1, 0, 0x28, dst, D1, l=1)                 # vmovaps ymm dst, ymm D1
    else:
        a.v(1, 0, 0x28, dst, D1)                      # vmovaps xmm dst, xmm D1 (zeroes the upper half)
    for i, x in enumerate(xs):
        a.v(1, 0, 0x10, x, _frame_slot(32 * i), l=1)  # vmovups ymm, [slot]
    a.mov_load(64, g, _frame_slot(32 * count))
    a.frame_out(F)


def _f2h(a, g, inp, out, tmp):
    """out = 4 halves (in 32-bit lanes, low 16 bits) rounded to nearest even from the 4 floats of inp."""
    A, B, C, E = inp, tmp[0], tmp[1], tmp[2]
    # A = float bits; B = sign; C/E temps; out accumulates
    _splat(a, g, E, 0x80000000)
    _vop(a, 0xdb, B, A, E)                            # B = sign bits
    _vop(a, 0xef, A, A, B)                            # A = |f| bits
    # normal path -> out
    _vshift(a, 2, C, A, 13)                           # C = A >> 13
    _splat(a, g, E, 1)
    _vop(a, 0xdb, C, C, E)                            # C = mantissa LSB (odd)
    _splat(a, g, E, 0xfff - (112 << 23) & 0xffffffff)
    _vop(a, 0xfe, out, A, E)                          # out = A + bias
    _vop(a, 0xfe, out, out, C)                        # + odd
    _vshift(a, 2, out, out, 13)                       # out = normal result
    # subnormal result: bits(|f| + 0.5f) - 0x3f000000
    _splat(a, g, E, 0x3f000000)
    a.v(1, 0, 0x58, C, E, vvvv=A)                     # vaddps C, A, 0.5f
    _vop(a, 0xfa, C, C, E)                            # vpsubd C, C, 0x3f000000
    _splat(a, g, E, 0x38800000)
    _vop(a, 0x66, E, E, A)                            # E = (0x38800000 > |f|) -> subnormal mask
    _vop(a, 0xdb, C, C, E)                            # C = subnormal & value
    _vop(a, 0xdf, E, E, out)                          # E = ~mask & normal
    _vop(a, 0xeb, out, C, E)                          # out = merged
    # overflow: |f| >= 0x47800000 -> 0x7c00
    _splat(a, g, E, 0x477fffff)
    _vop(a, 0x66, C, A, E)                            # C = |f| > 0x477fffff
    _splat(a, g, E, 0x7c00)
    _vop(a, 0xdf, out, C, out)                        # out = ~C & out
    _vop(a, 0xdb, C, C, E)                            # C = 0x7c00 where overflow
    _vop(a, 0xeb, out, out, C)
    # nan: |f| > 0x7f800000 -> 0x7e00 | payload bits (|f| >> 13 & 0x1ff)
    _splat(a, g, E, 0x7f800000)
    _vop(a, 0x66, C, A, E)                            # C = nan mask
    _vshift(a, 2, A, A, 13)
    _splat(a, g, E, 0x1ff)
    _vop(a, 0xdb, A, A, E)
    _splat(a, g, E, 0x7e00)
    _vop(a, 0xeb, A, A, E)                            # A = nan result
    _vop(a, 0xdb, A, A, C)                            # A = nan result & mask
    _vop(a, 0xdf, out, C, out)                        # out = ~nanmask & out
    _vop(a, 0xeb, out, out, A)
    # sign
    _vshift(a, 2, B, B, 16)
    _vop(a, 0xeb, out, out, B)


def _cvtps2ph(a, s):
    wide = s.size == 256
    src = s.reg
    dst_reg = None if isinstance(s.rm, Mem) else s.rm
    exclude = {src} | ({dst_reg} if dst_reg is not None else set())
    count = 7 if wide else 6
    xs = _pick_xmm(exclude, count)
    A, B, C, E, O1, O2 = xs[:6]
    H = xs[6] if wide else None
    (g,) = _pick(s.rm.regs() if isinstance(s.rm, Mem) else set())
    F = RED_ZONE + 32 * count + 8
    a.frame_in(F)
    for i, x in enumerate(xs):
        a.v(1, 0, 0x11, x, _frame_slot(32 * i), l=1)
    a.mov_store(64, _frame_slot(32 * count), g)
    a.v(1, 0, 0x28, A, src)                           # vmovaps xmm A, xmm src (low four floats)
    if wide:
        a.v(3, 1, 0x19, src, H, l=1, imm=b'\x01')     # vextractf128 H, ymm src, 1
    _f2h(a, g, A, O1, (B, C, E))
    if wide:
        a.v(1, 0, 0x28, A, H)
        _f2h(a, g, A, O2, (B, C, E))
    else:
        _vop(a, 0xef, O2, O2, O2)
    for o in (O1, O2):
        _vshift(a, 6, o, o, 16)                       # sign-extend 16 -> 32 so vpackssdw keeps all bits
        _vshift(a, 4, o, o, 16)
    _vop(a, 0x6b, O1, O1, O2)                         # vpackssdw: 8 halves (high half zero for 128-bit)
    if isinstance(s.rm, Mem):
        if wide:
            a.v(1, 2, 0x7f, O1, s.rm)                 # vmovdqu m128, xmm
        else:
            a.v(1, 1, 0xd6, O1, s.rm)                 # vmovq m64, xmm
    else:
        if wide:
            a.v(1, 1, 0x6f, dst_reg, O1)              # vmovdqa xmm dst, xmm (zeroes the upper half)
        else:
            a.v(1, 2, 0x7e, dst_reg, O1)              # vmovq xmm dst, xmm (zeroes bits 127:64 and above)
    for i, x in enumerate(xs):
        a.v(1, 0, 0x10, x, _frame_slot(32 * i), l=1)
    a.mov_load(64, g, _frame_slot(32 * count))
    a.frame_out(F)
