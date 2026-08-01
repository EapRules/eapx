"""Full-scale rehearsal for a large synthetic payload on Linux/Python 3.7.

1770 asset files, ~303 MB, plus a native library. Measures first install,
marker fast path, resume after an interrupt, and verify.
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zipfile

TARGET_FILES = int(os.environ.get("N_FILES", "1770"))
TARGET_BYTES = 303 * 1024 * 1024
ROOT = os.environ.get("SCALE_ROOT", "/tmp/scale")
GAME = os.path.join(ROOT, "game")
DATA = os.path.join(GAME, "gamedata")


def elf64_arm():
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    struct.pack_into("<H", header, 18, 183)
    return bytes(header) + os.urandom(4_499_404)  # same size as the real one


def build():
    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(DATA)
    per_file = TARGET_BYTES // TARGET_FILES
    # Semi-compressible, like real game assets: a random block repeated with
    # per-file variation, so the zip is not absurdly large but the bytes differ.
    block = os.urandom(per_file // 8 or 1)
    started = time.time()
    path = os.path.join(DATA, "NeededFiles.apk")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("AndroidManifest.xml", b"<manifest/>")
        archive.writestr("lib/arm64-v8a/libsample.so", elf64_arm())
        for index in range(TARGET_FILES):
            payload = (str(index).encode() + block) * 8
            archive.writestr(
                "assets/published/pack%02d/asset%04d.dat" % (index % 24, index),
                payload[:per_file],
            )
    size = os.path.getsize(path)
    print("built %s in %.1fs (%.0f MiB on disk, %d entries)"
          % (path, time.time() - started, size / 1048576.0, TARGET_FILES + 2))


RECIPE = {
    "schema": 1,
    "id": "synthetic-scale",
    "version": "1",
    "title": "Synthetic Scale Test",
    "abi_order": ["arm64-v8a"],
    "extract": [
        {
            "id": "native-library",
            "description": "the game's native library",
            "destination": "lib/{abi}/libsample.so",
            "source": {"kind": "entry",
                       "patterns": ["lib/{abi}/libsample.so"]},
            "validate": {"elf_machine": "{abi}", "min_size": 1024},
        },
        {
            "id": "published-assets",
            "description": "the published asset tree",
            "destination": "assets/published",
            "source": {"kind": "entries",
                       "patterns": ["assets/published/*"],
                       "strip_prefix": "assets/published/"},
            "validate": {"min_files": 50, "min_bytes": 200 * 1024 * 1024},
        },
    ],
    "commit": ["lib/{abi}/libsample.so", "assets/published"],
}


def run(label, *extra):
    argv = [sys.executable, os.environ.get("EAPX", "/tmp/eapx.py"), "install", "--recipe",
            os.path.join(ROOT, "recipe.json"), "--game-dir", GAME, "--quiet"]
    started = time.time()
    result = subprocess.run(list(argv) + list(extra),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = time.time() - started
    status = "ok" if result.returncode == 0 else "FAILED(%d)" % result.returncode
    print("%-28s %7.2fs  %s" % (label, elapsed, status))
    if result.returncode != 0:
        sys.stdout.write(result.stderr.decode()[:600] + "\n")
    return result.returncode, elapsed


def main():
    build()
    with open(os.path.join(ROOT, "recipe.json"), "w") as stream:
        json.dump(RECIPE, stream)

    code, first = run("first install")
    if code != 0:
        return 1

    installed = sum(
        os.path.getsize(os.path.join(base, name))
        for base, _dirs, files in os.walk(os.path.join(GAME, "assets"))
        for name in files
    )
    count = sum(len(files) for _b, _d, files in os.walk(os.path.join(GAME, "assets")))
    print("installed: %d files, %.0f MiB" % (count, installed / 1048576.0))

    run("second run (marker)")

    # Interrupt mid-extraction, then resume.
    marker = os.path.join(GAME, ".eapx-synthetic-scale.json")
    os.unlink(marker)
    shutil.rmtree(os.path.join(GAME, "assets"))
    proc = subprocess.Popen(
        [sys.executable, os.environ.get("EAPX", "/tmp/eapx.py"), "install", "--recipe",
         os.path.join(ROOT, "recipe.json"), "--game-dir", GAME, "--quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(max(first * 0.4, 1.0))
    proc.kill()
    proc.wait()
    print("%-28s %7s  killed mid-extraction" % ("interrupt", "-"))
    run("resume after kill")

    subprocess.run([sys.executable, os.environ.get("EAPX", "/tmp/eapx.py"), "verify", "--recipe",
                    os.path.join(ROOT, "recipe.json"), "--game-dir", GAME,
                    "--quiet"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
