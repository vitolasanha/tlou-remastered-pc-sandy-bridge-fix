#!/usr/bin/env python3
"""The Last of Us Remastered (CUSA00552): prepare the executable and start it in the runtime.

Without --boot it only lists the PS4 imports the runtime does not provide (--check-imports).
The update (a folder next to the game named like it plus -patch) is laid over the game: its
eboot.bin, sce_module and sce_sys are prepared, and the runtime reads its files over the game's.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPDATE_SUFFIXES = ("-patch", "-update")


def find_update(game):
    chosen = os.environ.get("TLOU_UPDATE", "")
    if chosen == "0":
        return None
    candidates = [Path(chosen)] if chosen else [game.parent / (game.name + s) for s in UPDATE_SUFFIXES]
    for candidate in candidates:
        if (candidate / "eboot.bin").is_file() and (candidate / "sce_sys/param.sfo").is_file():
            return candidate.resolve()
    return None


def app_version(folder):
    """APP_VER from the folder's sce_sys/param.sfo, or None."""
    sys.path.insert(0, str(ROOT / "gow3_pc/scripts"))
    try:
        from prepare import sfo
        return sfo((folder / "sce_sys/param.sfo").read_bytes()).get("APP_VER")
    except (OSError, ValueError, ImportError):
        return None
    finally:
        sys.path.pop(0)


def boot_view(base, update):
    """Copies of what the prepare scripts read (eboot.bin, sce_module, sce_sys), the update's
    over the game's, in analysis/views (copied again only when a source changes)."""
    chosen = {}
    for layer in (base, update) if update else (base,):
        for entry in layer.iterdir():
            if entry.is_file() and entry.name.lower() == "eboot.bin":
                chosen["eboot.bin"] = ("eboot.bin", entry)
            elif entry.is_dir() and entry.name.lower() in ("sce_module", "sce_sys"):
                for path in entry.rglob("*"):
                    if path.is_file():
                        relative = path.relative_to(layer)
                        chosen[str(relative).lower()] = (str(relative), path)
    view = ROOT / "analysis/views" / ("boot-" + (update or base).name)
    for relative, source in chosen.values():
        dest = view / relative
        wanted = source.stat()
        try:
            current = dest.stat()
            if current.st_size == wanted.st_size and abs(current.st_mtime - wanted.st_mtime) <= 2:
                continue
        except OSError:
            pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        os.utime(dest, ns=(wanted.st_atime_ns, wanted.st_mtime_ns))
    return view


def convert_frames(capture):
    """The presented frames (output_NNN_WxH.raw, RGBA) as PNG files next to them."""
    import re
    try:
        from PIL import Image
    except ImportError:
        print("Pillow is not installed: frames stay as .raw files")
        return
    for raw in sorted(capture.glob("output*.raw")):
        size = re.search(r"_(\d+)x(\d+)\.raw$", raw.name)
        if not size:
            continue
        w, h = int(size.group(1)), int(size.group(2))
        data = raw.read_bytes()
        if len(data) < w * h * 4:
            continue
        image = Image.frombytes("RGBA", (w, h), data[:w * h * 4]).convert("RGB")
        image.thumbnail((1280, 720))
        image.save(raw.with_suffix(".png"))
    print(f"Frames: {capture}")


# Changes to the game's executable before it starts: per change, image offsets (eboot vaddr), the
# bytes that must be there, and the bytes written instead (None: only checked).
# Cheats from the GoldHEN cheat repository, by Celogamez: json/CUSA00552_01.11.json
# (https://github.com/GoldHEN/GoldHEN_Cheat_Repository).
CHEATS = {
    # "Flashlight Does Not Turn Off": the flashlight's battery store (vmovss [r13+0x7d4], xmm2)
    # writes xmm1, the level before the drain, so the flashlight never flickers and needs no shake
    # of a DualShock 4's motion sensors. The cheat jumps to a code cave holding that instruction;
    # it is the same length, so it is written in place.
    "flashlight": [(0x697B98, "C4C17A1195D4070000", "C4C17A118DD4070000")],
}
# Idle job workers sleep (TLOU_JOB_SLEEP=0 keeps them spinning). The engine's 6 NdJobWorkerThreads spin on their job
# queues with `pause` unless the job manager's sleep flag (byte +0x59) is on; with it on, an idle
# worker waits on the manager's condition variable after 1000 spins (10 ms timeout) and every job
# submission wakes them. On a PS4 each worker owns a core; here they took about half of their time
# spinning (pause at 0xC87D65). The flag is treated as on in the worker loop, its re-check before
# waiting and the submit path, as in the Uncharted ports of the same engine.
JOB_SLEEP = [(0xC87D63, "7304", "EB04"), (0xC87D96, "741B", "9090"), (0xC873E1, "742D", "9090")]
# The PS4 Pro's shadows (TLOU_PRO_SHADOWS=0 keeps the base PS4's). On a Pro (sceKernelIsNeoMode, the
# engine's IsNeo at 0xB28670) the game sets the render options' (0x181A770) "Neo Enhancements" mode
# (+0x7FC: resolution, framerate, quality), which picks the resolution (+0xF0), checkerboard (+0x7D0)
# and "Toggle On High Detailed Mode" (+0x21D, on in framerate and quality). That flag is read only by
# the frame's shadow setup (0xFD31): the sun's cascades go to 4096x2048 instead of 2560x1280 (render
# target 0x3C instead of 0x3B), and the local lights' shadow memory, which the base PS4 always splits
# into 16 maps, is split by the number of lights casting shadows (targets 0x3D-0x41: 1 map of 4 times
# the side, 2, 4, 8 or 16). The base PS4 creates those targets too. The flag's default (the render
# options' initializer) is on; the rest of the Pro mode stays off (+0x42C, a faster depth clear, too).
PRO_SHADOWS = [(0xAEB313, "C6831D02000000", "C6831D02000001")]
# Fixes, on in every run.
FIXES = {
    # 23 Oct 2026 (the house, after the autosave before the basement): the renderer's frame
    # watchdog (ndgi-orbis-device.cpp WaitOnContextDone) halted the game when a frame took over 2 s
    # on the GPU. There the GPU command thread created ~150 new pipelines inside one frame (stack: a
    # DRAW_INDEX_OFFSET_2 opening a pipeline cache file on a hard disk). The compare (vucomiss
    # xmm0, [2.0f]) and its message (vmovsd xmm0, [2.0]) read a 20.0 of the read-only image
    # instead: a compile burst is a pause, a real GPU hang still stops the game.
    "frame-timeout": [(0xB3D887, "C5F82E05819F6300", "C5F82E05194D5C00"),
                      (0xB3D92F, "C5FB1005F19E6300", "C5FB1005A13D5900"),
                      (0x11025A8, "0000A041", None), (0x10D16D8, "0000000000003440", None)],
}


def image_patches(elf_path, out_path, patches):
    """Writes the chosen changes ({name: changes}, from CHEATS and FIXES) as a G3PATCH2 file for
    the runtime's --patches (src/probe.c apply_patches) and returns its path, or None. A change
    whose original bytes are not in this executable (another version) is left out with a message."""
    import struct
    elf = elf_path.read_bytes()
    phoff, = struct.unpack_from("<Q", elf, 0x20)
    phentsize, phnum = struct.unpack_from("<HH", elf, 0x36)
    segments = []
    for i in range(phnum):
        kind, _, offset, vaddr, _, filesz, _, _ = struct.unpack_from("<IIQQQQQQ", elf, phoff + i * phentsize)
        if kind == 1:
            segments.append((vaddr, offset, filesz))

    def read(vaddr, size):
        for start, offset, filesz in segments:
            if start <= vaddr and vaddr + size <= start + filesz:
                return elf[offset + vaddr - start:offset + vaddr - start + size]
        return b""

    entries = []
    for name, changes in patches.items():
        differ = [at for at, original, _ in changes if read(at, len(original) // 2) != bytes.fromhex(original)]
        if differ:
            print(f"Patches: '{name}' left out: this executable is not version 01.11's at {differ[0]:#x}")
            continue
        entries += [(at, bytes.fromhex(replacement)) for at, _, replacement in changes if replacement]
        print(f"Patches: {name} on")
    if not entries:
        return None
    data = b"G3PATCH2" + struct.pack("<QQ", 0, len(entries))
    for at, replacement in entries:
        data += struct.pack("<QQ", at, len(replacement)) + replacement
    out_path.write_bytes(data)
    return out_path


def game_executable():
    built = ROOT / "gow3_pc/out/gow3-probe.exe"
    if not built.is_file():
        raise SystemExit(f"Build the runtime first (gow3_pc/build.sh): {built} is missing")
    return built


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, default=ROOT.parent / "CUSA00552")
    parser.add_argument("--boot", action="store_true", help="Execute the game after preparation")
    parser.add_argument("--cpu-only", action="store_true")
    parser.add_argument("--seconds", type=int, default=60, help="Process time limit; 0 disables it")
    parser.add_argument("--capture", type=int, default=0, metavar="SECONDS",
                        help="Save the displayed frame every SECONDS (analysis/tlou/captures)")
    parser.add_argument("--saves", type=Path, default=None,
                        help="Saves and game settings folder (default analysis/tlou-user, the "
                             "player's): a test profile leaves the player's saves alone")
    args = parser.parse_args()
    game = args.game.resolve()
    update = find_update(game)
    if update:
        print(f"Game {game} (version {app_version(game)}) with its update {update} "
              f"(version {app_version(update)}): the update's executable and files are used", flush=True)
    else:
        print(f"Game {game} (version {app_version(game)}); no update found next to it "
              f"({game.name}-patch or {game.name}-update, or TLOU_UPDATE=<folder>)", flush=True)
    out = ROOT / "analysis/tlou"
    user = ROOT / "analysis/tlou-user"
    user.mkdir(parents=True, exist_ok=True)
    # Diagnostic switches (tlou_pad.py) last for one run: a new run starts with the defaults.
    for name in ("disabled-optimizations.txt", "skip.txt", "readback.trigger", "capture.trigger",
                 "probe-pad.txt"):
        (user / name).unlink(missing_ok=True)
    # The diagnostic files and the shader cache stay in analysis/tlou-user either way.
    saves = args.saves.resolve() if args.saves else user
    saves.mkdir(parents=True, exist_ok=True)
    view = boot_view(game, update)
    scripts = ROOT / "gow3_pc/scripts"
    for script in ("prepare.py", "link_modules.py", "content_profile.py"):
        subprocess.run([sys.executable, str(scripts / script), str(view), "--out", str(out)], check=True)
    # CPUs older than Haswell lack instructions the PS4 code uses (BMI1, LZCNT, MOVBE, F16C): they are
    # replaced by emulation code in the same table as the red-zone protection (gow3_pc/scripts/
    # isa_emulation.py). TLOU_ISA_REWRITE=auto (default) | force | off.
    sys.path.insert(0, str(scripts))
    import cpu_features
    isa_mode = os.environ.get("TLOU_ISA_REWRITE", "auto").lower()
    if isa_mode == "off":
        isa_names = []
    elif isa_mode == "force":
        isa_names = list(cpu_features.FEATURE_KINDS)
    else:
        isa_names = cpu_features.missing_emulatable() or []
    # Windows red-zone protection (gow3_pc/scripts/red_zone.py): GOW3_RED_ZONE_REWRITE=0 turns it off.
    red_zone = None
    want_red_zone = os.environ.get("GOW3_RED_ZONE_REWRITE") != "0"
    if want_red_zone or isa_names:
        command = [sys.executable, str(scripts / "red_zone_table.py"), str(out / "boot-linked.bin"), "--out", str(out)]
        if not want_red_zone:
            command.append("--isa-only")
        table = subprocess.run(command)
        if table.returncode == 0:
            red_zone = out / "red-zone.bin"
        elif isa_names:
            sys.exit("This CPU lacks " + ", ".join(isa_names) + ", which the game's code uses, and the table that "
                     "replaces those instructions could not be made (see above; it needs the iced_x86 package that "
                     "the release folder carries in gow3_pc/out/pydeps).")
    isa_active = bool(isa_names) and red_zone is not None
    if isa_active:
        print("ISA emulation active for: " + ", ".join(isa_names))

    env = dict(os.environ)
    if isa_active and not cpu_features.missing_blocking():
        env["GOW3_CPU_CHECK"] = "0"           # the runtime would stop on the sets that are emulated now
    msys = Path(os.environ.get("MSYS2_ROOT", "C:/msys64"))
    env["PATH"] = str(msys / "clang64/bin") + os.pathsep + env.get("PATH", "")
    env["GOW3_GPU_USER_DIR"] = str(user)
    # The shader cache: analysis/tlou-user/cache or, when that is a link (a junction to an SSD,
    # README), the folder it points to.
    cache = user / "cache"
    if cache.exists():
        env.setdefault("GOW3_GPU_CACHE_DIR", str(cache.resolve()))
    env.setdefault("GOW3_CONFIG", str(user / "tlou.ini"))
    # A draw whose pipeline is still compiling in the background is skipped: the curtain on the
    # title screen and the characters' hair flashed white the first time they were drawn, and the
    # stale data skipped draws leave crashed the game (two device losses in the Quarantine Zone,
    # a game assertion; 22 Oct 2026). New pipelines are compiled at once (a short stall the first
    # time; the pipeline cache and the prefetch below keep it short), whatever the settings file
    # says, and the in-game menu does not offer the switch.
    env.setdefault("GOW3_ASYNC_SHADERS", "0")
    env.setdefault("GOW3_ASYNC_SHADERS_LOCKED", "1")
    # The game compares sceKernelGetDirectMemorySize with a retail console's 4608 MiB; any other
    # size makes it reserve a development kit's 768 MiB debug area.
    env.setdefault("GOW3_DMEM_REPORT_MB", "4608")
    env.setdefault("GOW3_DIRECT_MEMORY_ACCESS", "1")
    env.setdefault("GOW3_PIPELINE_CACHE", "1")
    env.setdefault("GOW3_PIPELINE_BINARIES", "1")
    # Frame rate (Sarah's bedroom, GTX 1050 Ti Mobile: 20.6 FPS with none of these, 47 with all
    # and the priority below; 48-53 walking upstairs): multithreaded rendering (draw and Vulkan
    # recording threads), 28 FPS on its own with the DMA windows; the DMA shaders' guest
    # memory synchronized in 256 KiB windows at the addresses in their user data instead of every
    # CPU-dirty range; and one frame in flight, the game building the next frame while the GPU
    # command thread still decodes the last one, instead of both waiting for the other at each
    # sceGnmSubmitDone (liverpool.h, FramesInFlight).
    env.setdefault("GOW3_DRAW_PIPE", "1")
    env.setdefault("GOW3_VK_RECORD_THREAD", "1")
    env.setdefault("GOW3_DMA_SYNC", "pointers 256")
    env.setdefault("GOW3_FRAMES_IN_FLIGHT", "1")
    # At normal priority the GPU command thread and the draw and Vulkan recording threads shared
    # cores with the game's six busy job workers in some sessions, for the whole session: 33-34
    # instead of 46-47 FPS in 5 of 11 runs. Above normal: 47 FPS in 4 runs of 4.
    env.setdefault("GOW3_GPU_THREAD_PRIORITY", "above")
    # A game thread's write into memory the GPU is still writing waits for that GPU work itself,
    # not on the GPU command thread (which stopped drawing meanwhile): 32-36 -> 41-47 FPS in the
    # bedroom (4 alternating runs, 22 Oct 2026). Sarah's hair shakes with and without it.
    env.setdefault("GOW3_FETCH_NO_BLOCK", "1")
    # New pipelines the draw preparation workers see ahead are built on the compiler threads, in
    # parallel, before the GPU command thread reaches their draws (an empty cache: 573 -> 167 ms
    # of compiling on that thread). The first visit of an area hitches less.
    env.setdefault("GOW3_PREFETCH_PIPELINES", "1")
    # Memory waits met by a fence still queued for the draw recording thread, nested compute
    # buffers and compute memory waits do not drain the draw pipe (in the heavy part of the house
    # the GPU command thread waited for that thread 47% of its time). The player's choice (22 Oct
    # 2026) after playing it: it felt smoother there; the measured runs saw different parts of
    # the scene, so no clean number.
    env.setdefault("GOW3_FEWER_DRAINS", "1")
    # The 480 Hz vblank (frames shown when they are done, the frame rate capped at the display's):
    # at a 60 Hz vblank the game waits for the next one, so frames of the heavy areas took
    # 16.7/33.3/50 ms in turn (~30 FPS). The player's session with it (24 Oct 2026): 30-34 FPS
    # there, normal game speed. (Joel's hair still stretches with it at times: not the vblank.)
    env.setdefault("GOW3_VBLANK_HZ", "0")
    # A game thread's write beside GPU bytes still being written (the copy shader 82c46b8b's
    # constant blocks in the per-frame rings) goes ahead instead of waiting ~37 ms for the frame
    # in flight; those GPU bytes are poisoned in guest memory until their data lands. Pages of
    # the physics shaders (results the game reads) still wait. The player's sessions in the
    # Quarantine Zone (24 Oct 2026): frame time stddev ~15 -> ~10 ms, slow frames 41-72 -> 5-19
    # per 5 s, same FPS; GOW3_SHARE_CHECK=1 found no stale or poisoned bytes.
    env.setdefault("GOW3_SHARE_UNFINISHED_WRITES", "1")
    # The small GPU-side DMA copies into guest memory (counters the game reads) come back with
    # the next end-of-pipe fence, held until they land, so writes beside them need not wait
    # either. Shared without it, the NPCs walked oddly. The player's sessions (25 Oct 2026):
    # NPCs normal in 2 of 3 (rising in the first, with no difference found in its log), stddev
    # ~11 -> ~8 ms, slow frames ~12 -> ~2 per 5 s. GOW3_DMA_EOP_READBACK=0: those pages wait.
    env.setdefault("GOW3_DMA_EOP_READBACK", "1")
    env.setdefault("GOW3_NATIVE_TSC", "1")
    env.setdefault("GOW3_FRAME_STATS", "1")
    # Scripted controller input (tools/tlou_pad.py writes it).
    env.setdefault("GOW3_PAD_FILE", str(user / "probe-pad.txt"))
    # 24 Oct 2026: the player's sessions record their controller input from the first press
    # (GOW3_PAD_RECORD, flushed as it goes), one file per session, the newest 5 kept: a device loss
    # came minutes into a level only on some routes, and a recording replays the route in a test
    # profile (GOW3_PAD_REPLAY). Test runs (--saves) record nothing; TLOU_RECORD_PAD=0 turns it off.
    if not args.saves and os.environ.get("TLOU_RECORD_PAD") != "0" and "GOW3_PAD_RECORD" not in env:
        records = sorted(out.glob("pad-record-*.txt"))
        for old in records[:-4]:
            old.unlink(missing_ok=True)
        env["GOW3_PAD_RECORD"] = str(out / f"pad-record-{time.strftime('%Y%m%d-%H%M%S')}.txt")
        (user / "probe-pad.txt").write_text("record", encoding="ascii")
    # Diagnostics driven by tlou_pad.py: the frame analyzer (every pass, its shaders and textures,
    # written to <user>/captures) and shaders whose draws are skipped (hex hashes, re-read live).
    env.setdefault("GOW3_CAPTURE_TRIGGER", str(user / "capture.trigger"))
    # The game never draws into its display buffers, so the analyzer sees no frame boundary: it
    # records this many passes (a number in the trigger file overrides it).
    env.setdefault("GOW3_CAPTURE_PASSES", "400")
    env.setdefault("GOW3_SKIP_FILE", str(user / "skip.txt"))
    # "<lo> <hi>" (hex) in this file copies that GPU-written range back to guest memory once.
    env.setdefault("GOW3_READBACK_TRIGGER", str(user / "readback.trigger"))
    # Tiny GPU-written images (one row, at most 4 KiB) return to guest memory with the next fence:
    # the aim focus distance may be read from a 1x1 target; never copied back, it stays 0.
    env.setdefault("GOW3_TINY_IMAGE_READBACK", "1")
    # The GPU command thread is the limit in heavy areas (0% idle, ~29 FPS). Submissions reuse their
    # buffers instead of freeing them (~16% of that thread) and small per-draw savings: 39.9 -> 43.4 FPS.
    env.setdefault("GOW3_PREP_POOL", "1")
    env.setdefault("GOW3_CPU_TRIMS", "1")
    # The game's motion blur off (GOW3_NO_MOTION_BLUR=0 keeps it): its shaders see no movement.
    env.setdefault("GOW3_NO_MOTION_BLUR", "1")
    # Optimizations switched off while the game runs (a bit mask, src/runtime_memory.c).
    env.setdefault("GOW3_TOGGLE_FILE", str(user / "disabled-optimizations.txt"))
    if os.environ.get("TLOU_GPU_CHECKPOINTS") == "1":
        env.setdefault("GOW3_GPU_CHECKPOINTS", "1")

    command = [str(game_executable()), str(out / "boot-linked.bin"),
               "--no-game-hooks", "--low-image", "--content-profile", str(out / "content.bin"),
               "--app0", str(game), "--user", str(saves)]
    if update:
        command += ["--app0-overlay", str(update)]
    command += ["--guest-path", "/app0/eboot.bin"]
    # TLOU_CHEATS=<name>,...: the cheats above (default: flashlight; "none": no cheats).
    # TLOU_FIXES=none leaves out the fixes.
    patches = dict(FIXES) if os.environ.get("TLOU_FIXES") != "none" else {}
    if os.environ.get("TLOU_JOB_SLEEP") != "0":
        patches["job-sleep"] = JOB_SLEEP
    if os.environ.get("TLOU_PRO_SHADOWS") != "0":
        patches["pro-shadows"] = PRO_SHADOWS
    for name in [c.strip() for c in os.environ.get("TLOU_CHEATS", "flashlight").split(",") if c.strip()]:
        if name in CHEATS:
            patches[name] = CHEATS[name]
        elif name != "none":
            print(f"Cheats: unknown '{name}' (known: {', '.join(CHEATS)})")
    if patches:
        patch_file = image_patches(out / "eboot.elf", out / "patches.bin", patches)
        if patch_file:
            command += ["--patches", str(patch_file)]
    if red_zone:
        command += ["--red-zone", str(red_zone)]
    if not args.boot:
        command.append("--check-imports")
    if args.cpu_only:
        command.append("--cpu-only")
    name = ("boot" if args.boot else "check-imports") + ("-cpu" if args.cpu_only else "")
    result = {"command": command, "update": str(update) if update else None, "timeout_seconds": args.seconds}
    capture = None
    if args.capture:
        # The presenter writes the displayed frame each time the trigger file appears.
        capture = out / "captures" / str(time.time_ns())
        capture.mkdir(parents=True)
        env["GOW3_FRAME_DUMP_TRIGGER"] = str(capture / "trigger")
        env["GOW3_DUMP_DIR"] = str(capture)
        result["capture_directory"] = str(capture)
    start = time.monotonic()
    with (out / (name + ".log")).open("wb") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        # Test scripts stop this instance by its PID, never a game the player started.
        (out / "probe.pid").write_text(str(process.pid), encoding="ascii")
        deadline = start + args.seconds if args.seconds else None
        result["timed_out"] = False
        while process.poll() is None:
            if deadline and time.monotonic() >= deadline:
                process.kill()
                result["timed_out"] = True
                break
            try:
                process.wait(timeout=args.capture or 1)
            except subprocess.TimeoutExpired:
                if capture:
                    (capture / "trigger").touch()
        result["exit_code"] = process.wait()
    if capture:
        convert_frames(capture)
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    (out / (name + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    attempt = out / "attempts" / f"{name}-{time.time_ns()}"
    attempt.mkdir(parents=True)
    shutil.copy2(out / (name + ".log"), attempt / (name + ".log"))
    print(json.dumps(result, indent=2))
    print(f"Log: {out / (name + '.log')}")
    return 124 if result["timed_out"] else result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
