"""Clear the executable-stack flag ctranslate2's bundled .so ships with.

ctranslate2<4.5 wheels mark PT_GNU_STACK as RWE (a linked component is
missing a .note.GNU-stack section). Newer glibc/kernel combos refuse the
mprotect(PROT_EXEC) this requires at dlopen time, failing every import with
"cannot enable executable stack as shared object requires: Invalid argument".
ctranslate2 doesn't actually run code off the stack, so clearing the flag
(same edit the `execstack -c` tool makes) is safe.
"""
import glob
import struct
import sys

PT_GNU_STACK = 0x6474E551
PF_X = 1


def clear_exec_flag(path: str) -> bool:
    with open(path, "rb") as f:
        data = bytearray(f.read())

    if data[:4] != b"\x7fELF" or data[4] != 2:
        return False

    e_phoff, = struct.unpack_from("<Q", data, 0x20)
    e_phentsize, = struct.unpack_from("<H", data, 0x36)
    e_phnum, = struct.unpack_from("<H", data, 0x38)

    changed = False
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, p_flags = struct.unpack_from("<II", data, off)
        if p_type == PT_GNU_STACK and p_flags & PF_X:
            struct.pack_into("<I", data, off + 4, p_flags & ~PF_X)
            changed = True

    if changed:
        with open(path, "wb") as f:
            f.write(data)
    return changed


if __name__ == "__main__":
    targets = glob.glob(sys.argv[1])
    if not targets:
        sys.exit(f"no files matched {sys.argv[1]!r}")
    for path in targets:
        print(f"{path}: {'cleared' if clear_exec_flag(path) else 'no change'}")
