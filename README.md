# THE LAST OF US REMASTERED (PC) – SANDY BRIDGE SUPPORT
========================================================

## THE ORIGINAL PROBLEM

The game was designed for the PS4's Jaguar CPU, which supports four instruction sets that Sandy Bridge processors lack: BMI1, LZCNT, MOVBE, and F16C.

The port executes the game's code directly on the host CPU, so these missing instructions caused illegal instruction errors. Worse, some instructions (such as TZCNT/LZCNT) could execute as BSF/BSR instead, silently producing incorrect results.

## WHAT WAS DONE

- `gow3_pc/scripts/isa_emulation.py` – Replaces each unsupported instruction with an equivalent sequence using only instructions supported by Sandy Bridge (AVX1/SSE/basic x86-64), preserving the same results and flags without modifying the stack's red zone.

- `gow3_pc/scripts/red_zone.py` / `red_zone_table.py` – Locate these instructions in the game executable and generate the table that the runtime already knows how to apply (`red-zone.bin`). The runtime `.exe` itself remains unchanged.

- `gow3_pc/scripts/cpu_features.py` – Detects which CPU features are missing and enables emulation only when necessary. Newer CPUs remain unaffected.

- `tools/probe_tlou.py` – Generates the instruction replacement table and disables the runtime's "unsupported CPU" check.

## HOW TO USE

To install the fix, simply copy the `gow3_pc` and `tools` folders from the overlay ZIP and paste them into the folder containing the latest release, allowing files to be replaced when prompted.

The runtime (`gow3-probe.exe`) is already compiled and included in the project's release ZIP, along with Python and `iced_x86`.

1. Download the latest release ZIP and extract it into a folder with no accented characters in its path.

2. Copy the `gow3_pc` and `tools` folders from the overlay ZIP and paste them into the extracted release folder, replacing existing files when prompted.

3. (Optional) Run `Diagnostico-CPU.cmd`. On your CPU, the output should look something like this:

   ```text
   Emulated:  BMI1, LZCNT, MOVBE, F16C
   Blocking: nothing
   ```

4. Open `TLOU Launcher.exe`, select the game, and click **PLAY**.

5. The first launch generates the instruction replacement table and may take a few extra minutes while Python analyzes the game executable. Subsequent launches reuse the generated table and only regenerate it if the game or its update changes.

## OPTIONAL ENVIRONMENT VARIABLE

`TLOU_ISA_REWRITE` supports three values:

- `auto` (default): Automatically enables emulation when required.
- `force`: Emulates all supported instructions, even on newer CPUs, for testing purposes.
- `off`: Disables instruction emulation.
