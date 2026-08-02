"""Tests for eapx. See DESIGN.md section 13.

These target the failure modes the design was built to avoid, not the happy
path: interrupted commits, interrupted rollbacks, fat APKs, zip-format OBBs,
foreign files under an install root, and recipes that lie.
"""

import json
import hashlib
import os
import re
import shutil
import struct
import tempfile
import unittest
import zipfile
from unittest import mock

import eapx


def elf(machine, ei_class, payload=b"\x00" * 200):
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = ei_class
    header[5] = 1
    struct.pack_into("<H", header, 18, machine)
    return bytes(header) + payload


ARM64_SO = elf(183, 2, b"arm64 payload")
ARM32_SO = elf(40, 1, b"arm32 payload")


def make_zip(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="eapx-test-")
        self.game = os.path.join(self.root, "game")
        self.data = os.path.join(self.game, "gamedata")
        os.makedirs(self.data)
        self.recipe_path = os.path.join(self.root, "recipe.json")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def recipe(self, **overrides):
        base = {
            "schema": 1,
            "id": "test-port",
            "version": "1",
            "abi_order": ["arm64-v8a", "armeabi-v7a"],
            "extract": [
                {
                    "id": "native-library",
                    "destination": "lib/{abi}/libgame.so",
                    "source": {"kind": "entry",
                               "patterns": ["lib/{abi}/libgame.so"]},
                    "validate": {"elf_machine": "{abi}", "min_size": 1},
                },
                {
                    "id": "game-assets",
                    "destination": "assets",
                    "source": {"kind": "entries", "patterns": ["assets/*"],
                               "strip_prefix": "assets/"},
                    "validate": {"min_files": 1},
                },
            ],
            "commit": ["lib/{abi}/libgame.so", "assets"],
        }
        base.update(overrides)
        return base

    def write_recipe(self, data=None):
        with open(self.recipe_path, "w") as stream:
            json.dump(data if data is not None else self.recipe(), stream)
        return self.recipe_path

    def apk_entries(self, abis=("arm64-v8a",)):
        entries = {"AndroidManifest.xml": b"<manifest/>"}
        for abi in abis:
            entries["lib/%s/libgame.so" % abi] = (
                ARM64_SO if abi == "arm64-v8a" else ARM32_SO
            )
        entries["assets/level.dat"] = b"level data"
        entries["assets/texture.bin"] = b"texture data"
        return entries

    def install(self, *extra):
        argv = ["install", "--recipe", self.recipe_path,
                "--game-dir", self.game, "--quiet"] + list(extra)
        return eapx.main(argv)

    def assert_installed(self, abi="arm64-v8a"):
        so = os.path.join(self.game, "lib", abi, "libgame.so")
        self.assertTrue(os.path.isfile(so), "missing %s" % so)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "assets/level.dat")))


class InstallTests(Base):
    def test_installs_from_a_renamed_apk(self):
        self.write_recipe()
        make_zip(os.path.join(self.data, "whatever.bin"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        self.assert_installed()

    def test_fat_apk_with_two_abis_picks_the_preferred_one(self):
        """The regression that matters: this used to fail as 'ambiguous'."""
        self.write_recipe()
        make_zip(os.path.join(self.data, "game.apk"),
                 self.apk_entries(abis=("arm64-v8a", "armeabi-v7a")))
        self.assertEqual(self.install(), 0)
        self.assert_installed("arm64-v8a")
        self.assertFalse(
            os.path.exists(os.path.join(self.game, "lib/armeabi-v7a/libgame.so"))
        )

    def test_zip_format_obb_can_be_copied_whole(self):
        recipe = self.recipe()
        recipe["extract"].append({
            "id": "obb",
            "destination": "main.obb",
            "source": {"kind": "blob", "patterns": ["*.obb"]},
            "validate": {"min_size": 1},
        })
        recipe["commit"].append("main.obb")
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        # A zip-format OBB, which is what Unity and Unreal actually ship.
        make_zip(os.path.join(self.data, "main.123.obb"), {"pack/a.dat": b"obb data"})
        self.assertEqual(self.install(), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "main.obb")))

    def test_second_run_uses_the_marker(self):
        self.write_recipe()
        source = os.path.join(self.data, "game.apk")
        make_zip(source, self.apk_entries())
        self.assertEqual(self.install(), 0)
        os.unlink(source)
        self.assertEqual(self.install(), 0, "second run must not need the package")
        self.assert_installed()

    def test_elf_class_mismatch_is_rejected(self):
        """A 32-bit ARMv5 object must not validate as arm64-v8a."""
        recipe = self.recipe()
        recipe["abi_order"] = ["arm64-v8a"]
        self.write_recipe(recipe)
        entries = self.apk_entries()
        entries["lib/arm64-v8a/libgame.so"] = ARM32_SO
        make_zip(os.path.join(self.data, "game.apk"), entries)
        self.assertEqual(self.install(), 1)

    def test_two_different_versions_are_ambiguous(self):
        self.write_recipe()
        make_zip(os.path.join(self.data, "v1.apk"), self.apk_entries())
        other = self.apk_entries()
        other["assets/level.dat"] = b"different level data"
        make_zip(os.path.join(self.data, "v2.apk"), other)
        self.assertEqual(self.install(), 1)

    def test_a_corrupt_file_does_not_poison_the_run(self):
        self.write_recipe()
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        with open(os.path.join(self.data, "broken.apk"), "wb") as stream:
            stream.write(b"PK\x03\x04 this is not really a zip")
        self.assertEqual(self.install(), 0)
        self.assert_installed()


def axml(package):
    """A minimal but structurally real binary AndroidManifest.xml."""
    strings = ["manifest", "package", package]
    blob, offsets = b"", []
    for text in strings:
        raw = text.encode("utf-8")
        offsets.append(len(blob))
        blob += bytes([len(raw), len(raw)]) + raw + b"\x00"
    blob += b"\x00" * (-len(blob) % 4)
    header = 28 + 4 * len(strings)
    pool = (
        struct.pack("<HHIIIIII", 0x0001, 28, header + len(blob), len(strings), 0,
                    0x100, header, 0)
        + b"".join(struct.pack("<I", o) for o in offsets)
        + blob
    )
    element = struct.pack(
        "<HHIIIIIHHHHHH", 0x0102, 16, 56, 1, 0xFFFFFFFF, 0xFFFFFFFF, 0,
        20, 20, 1, 0, 0, 0
    ) + struct.pack("<IIIII", 0xFFFFFFFF, 1, 2, (8 << 16) | (3 << 24), 2)
    body = pool + element
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


class ManifestTests(unittest.TestCase):
    def test_reads_the_package_name(self):
        self.assertEqual(eapx.android_package(axml("org.example.synthetic")),
                         "org.example.synthetic")

    def test_truncation_never_raises(self):
        """Malformed input must degrade, not print a traceback on the console.

        Truncating at every possible offset: the parser may return the package
        or give up, but it may never let an exception escape -- the parser this
        replaces threw IndexError and struct.error that no handler caught.
        """
        full = axml("org.example.synthetic")
        for cut in range(len(full)):
            try:
                result = eapx.android_package(full[:cut])
            except Exception as error:
                self.fail("a %d-byte prefix raised %r" % (cut, error))
            self.assertIn(result, (None, "org.example.synthetic"),
                          "a %d-byte prefix produced %r" % (cut, result))

    def test_corrupted_bytes_never_raise(self):
        full = bytearray(axml("org.example.synthetic"))
        for index in range(len(full)):
            mutated = bytearray(full)
            mutated[index] ^= 0xFF
            try:
                eapx.android_package(bytes(mutated))
            except Exception as error:
                self.fail("flipping byte %d raised %r" % (index, error))

    def test_garbage_is_rejected(self):
        for blob in (b"", b"not xml", b"\x03\x00\x08\x00" + b"\xff" * 40):
            self.assertIsNone(eapx.android_package(blob))


class BundleTests(Base):
    """XAPK and split APKs -- no port in the PortMaster catalogue handles these."""

    def inner_apk(self, entries, package="com.example.game"):
        import io
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("AndroidManifest.xml", axml(package))
            for name, data in entries.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    def test_xapk_with_a_single_inner_apk(self):
        self.write_recipe()
        entries = self.apk_entries()
        del entries["AndroidManifest.xml"]
        make_zip(os.path.join(self.data, "game.xapk"),
                 {"base.apk": self.inner_apk(entries)})
        self.assertEqual(self.install(), 0)
        self.assert_installed()

    def test_plan_reaches_an_inner_apk(self):
        import contextlib, io
        self.write_recipe()
        entries = self.apk_entries()
        del entries["AndroidManifest.xml"]
        make_zip(os.path.join(self.data, "game.xapk"),
                 {"base.apk": self.inner_apk(entries)})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = eapx.main(["plan", "--recipe", self.recipe_path,
                                "--game-dir", self.game, "--quiet"])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["items"]), 3)

    def test_xapk_with_abi_split(self):
        """The library lives in a config split, the assets in the base."""
        self.write_recipe()
        make_zip(os.path.join(self.data, "game.xapk"), {
            "base.apk": self.inner_apk({"assets/level.dat": b"level data",
                                        "assets/texture.bin": b"texture data"}),
            "config.arm64_v8a.apk": self.inner_apk(
                {"lib/arm64-v8a/libgame.so": ARM64_SO}),
        })
        self.assertEqual(self.install(), 0)
        self.assert_installed()

    def test_loose_splits_are_grouped_by_package(self):
        """base.apk and config.arm64_v8a.apk dropped straight into gamedata/."""
        self.write_recipe()
        with open(os.path.join(self.data, "base.apk"), "wb") as stream:
            stream.write(self.inner_apk({"assets/level.dat": b"level data",
                                         "assets/texture.bin": b"texture data"}))
        with open(os.path.join(self.data, "split_config.arm64_v8a.apk"), "wb") as stream:
            stream.write(self.inner_apk({"lib/arm64-v8a/libgame.so": ARM64_SO}))
        self.assertEqual(self.install(), 0)
        self.assert_installed()

    def test_two_games_are_not_merged(self):
        """Different packages must stay separate rather than be combined."""
        self.write_recipe()
        base = self.apk_entries()
        del base["AndroidManifest.xml"]
        with open(os.path.join(self.data, "a.apk"), "wb") as stream:
            stream.write(self.inner_apk(base, package="com.one"))
        other = dict(base)
        other["assets/level.dat"] = b"a different game entirely"
        with open(os.path.join(self.data, "b.apk"), "wb") as stream:
            stream.write(self.inner_apk(other, package="com.two"))
        self.assertEqual(self.install(), 1)

    def test_bundle_cache_is_removed_after_a_successful_install(self):
        self.write_recipe()
        entries = self.apk_entries()
        del entries["AndroidManifest.xml"]
        make_zip(os.path.join(self.data, "game.xapk"),
                 {"base.apk": self.inner_apk(entries)})
        self.assertEqual(self.install(), 0)
        cache = os.path.join(self.game, ".eapx", "test-port", "cache")
        self.assertFalse(os.path.exists(cache), "the unpack cache was left behind")


class DirectorySourceTests(Base):
    """A non-zip donor can be unpacked externally into an exploded APK tree."""

    def explode(self, root):
        for name, data in self.apk_entries().items():
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as stream:
                stream.write(data)

    def test_installs_from_an_unpacked_folder(self):
        self.write_recipe()
        self.explode(os.path.join(self.data, "NeededFiles"))
        self.assertEqual(self.install(), 0)
        self.assert_installed()

    def test_applestyle_sidecar_files_are_ignored(self):
        """A card prepared on a Mac carries ._ files next to every real one."""
        self.write_recipe()
        root = os.path.join(self.data, "NeededFiles")
        self.explode(root)
        for base, _dirs, files in list(os.walk(root)):
            for name in list(files):
                with open(os.path.join(base, "._" + name), "wb") as stream:
                    stream.write(b"\x00\x05\x16\x07apple double")
        self.assertEqual(self.install(), 0)
        self.assert_installed()
        stray = [
            name for _b, _d, files in os.walk(os.path.join(self.game, "assets"))
            for name in files if name.startswith("._")
        ]
        self.assertEqual(stray, [], "AppleDouble sidecars leaked into the install")

    def test_second_run_from_a_folder_uses_the_marker(self):
        self.write_recipe()
        root = os.path.join(self.data, "NeededFiles")
        self.explode(root)
        self.assertEqual(self.install(), 0)
        shutil.rmtree(root)
        self.assertEqual(self.install(), 0)


class DonorCompositionTests(Base):
    """Candidate roots and validation order for APK plus external data."""

    def write_tree_file(self, root, relative, data=b"external data"):
        path = os.path.join(root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(data)
        return path

    def test_immediate_sibling_directory_augments_an_apk_rule(self):
        recipe = self.recipe()
        recipe["extract"][1]["validate"]["min_files"] = 3
        recipe["validate"] = [{"path": "assets", "min_files": 3}]
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "base.apk"), self.apk_entries())
        self.write_tree_file(
            os.path.join(self.data, "external-data"),
            "assets/published/chapter.dat",
        )

        self.assertEqual(self.install(), 0)
        self.assertTrue(os.path.isfile(
            os.path.join(self.game, "assets/published/chapter.dat")
        ))

    def test_repeated_explicit_inputs_form_the_same_donor_group(self):
        recipe = self.recipe()
        recipe["extract"][1]["validate"]["min_files"] = 3
        self.write_recipe(recipe)
        apk = os.path.join(self.root, "phone-backup", "base.apk")
        external = os.path.join(self.root, "phone-data")
        make_zip(apk, self.apk_entries())
        self.write_tree_file(external, "assets/published/chapter.dat")

        self.assertEqual(
            self.install("--input", apk, "--input", external), 0
        )
        self.assert_installed()

    def test_discovery_does_not_open_an_apk_nested_in_a_directory(self):
        self.write_recipe()
        wrapper = os.path.join(self.data, "phone-backup")
        make_zip(os.path.join(wrapper, "base.apk"), self.apk_entries())

        self.assertEqual(
            self.install(), 1,
            "a nested APK must not become a recursive candidate",
        )

    def test_optional_rule_still_rejects_a_nonempty_partial_match(self):
        recipe = self.recipe()
        assets = recipe["extract"][1]
        assets["required"] = False
        assets["validate"]["min_files"] = 3
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "base.apk"), self.apk_entries())

        self.assertEqual(self.install(), 1)
        with open(os.path.join(self.game, "eapx.log")) as stream:
            log = stream.read()
        self.assertIn("game-assets: 2 file(s), expected at least 3", log)

    def test_top_level_validation_checks_a_tree_assembled_by_two_rules(self):
        recipe = self.recipe(
            extract=[
                {
                    "id": "native-library",
                    "destination": "lib/{abi}/libgame.so",
                    "source": {
                        "kind": "entry",
                        "patterns": ["lib/{abi}/libgame.so"],
                    },
                    "validate": {"elf_machine": "{abi}", "min_size": 1},
                },
                {
                    "id": "base-assets",
                    "required": False,
                    "destination": "assets",
                    "source": {
                        "kind": "entries",
                        "patterns": ["assets/base/*"],
                        "strip_prefix": "assets/base/",
                    },
                },
                {
                    "id": "external-assets",
                    "required": False,
                    "destination": "assets",
                    "source": {
                        "kind": "entries",
                        "patterns": ["published/*"],
                        "strip_prefix": "published/",
                    },
                },
            ],
            validate=[{"path": "assets", "min_files": 2}],
        )
        self.write_recipe(recipe)
        entries = {
            "AndroidManifest.xml": b"<manifest/>",
            "lib/arm64-v8a/libgame.so": ARM64_SO,
            "assets/base/level.dat": b"level data",
        }
        make_zip(os.path.join(self.data, "base.apk"), entries)
        self.write_tree_file(
            os.path.join(self.data, "external-data"),
            "published/texture.bin",
            b"texture data",
        )

        self.assertEqual(self.install(), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "assets/level.dat")))
        self.assertTrue(os.path.isfile(os.path.join(self.game, "assets/texture.bin")))


class ForeignFileTests(Base):
    def test_foreign_file_under_a_commit_root_blocks_the_install(self):
        self.write_recipe()
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        os.makedirs(os.path.join(self.game, "assets"))
        save = os.path.join(self.game, "assets", "savegame.sav")
        with open(save, "w") as stream:
            stream.write("precious")
        self.assertEqual(self.install(), 1)
        with open(save) as stream:
            self.assertEqual(stream.read(), "precious", "the save was destroyed")

    def test_exclusive_root_allows_replacement(self):
        recipe = self.recipe()
        recipe["commit"] = ["lib/{abi}/libgame.so",
                            {"path": "assets", "exclusive": True}]
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        os.makedirs(os.path.join(self.game, "assets"))
        with open(os.path.join(self.game, "assets", "stale.dat"), "w") as stream:
            stream.write("old")
        self.assertEqual(self.install(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.game, "assets/stale.dat")))


class RecipeTests(Base):
    def test_unknown_key_is_an_error(self):
        recipe = self.recipe()
        recipe["extract"][0]["validate"]["min_bites"] = 1
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError) as caught:
            eapx.Recipe(self.recipe_path)
        self.assertIn("min_bites", str(caught.exception))
        self.assertIn("did you mean", str(caught.exception))

    def test_file_validator_on_a_tree_rule_is_an_error(self):
        recipe = self.recipe()
        recipe["extract"][1]["validate"]["sha256"] = "a" * 64
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError) as caught:
            eapx.Recipe(self.recipe_path)
        self.assertIn("single-file", str(caught.exception))

    def test_contradictory_validators_are_rejected(self):
        recipe = self.recipe()
        recipe["extract"][0]["validate"] = {"min_size": 200, "max_size": 100}
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError):
            eapx.Recipe(self.recipe_path)

    def test_destination_outside_commit_root_is_rejected(self):
        recipe = self.recipe()
        recipe["commit"] = ["assets"]
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError) as caught:
            eapx.Recipe(self.recipe_path)
        self.assertIn("outside every commit root", str(caught.exception))

    def test_marker_under_a_commit_root_is_rejected(self):
        recipe = self.recipe()
        recipe["marker"] = "assets/.marker.json"
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError) as caught:
            eapx.Recipe(self.recipe_path)
        self.assertIn("commit root", str(caught.exception))

    def test_string_boolean_is_rejected(self):
        recipe = self.recipe()
        recipe["extract"][0]["required"] = "false"
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError):
            eapx.Recipe(self.recipe_path)


class CrashTests(Base):
    """Interrupt the transaction at every step and re-run."""

    def prepare_reinstall(self):
        """Install once, then set up a second install over the existing tree."""
        self.write_recipe()
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        # Change the payload so the second install has real work to do and the
        # old content is distinguishable from the new.
        entries = self.apk_entries()
        entries["assets/level.dat"] = b"level data v2"
        entries["assets/texture.bin"] = b"texture data v2"
        os.unlink(os.path.join(self.data, "game.apk"))
        make_zip(os.path.join(self.data, "game2.apk"), entries)
        os.unlink(os.path.join(self.game, ".eapx-test-port.json"))

    def read_level(self):
        path = os.path.join(self.game, "assets", "level.dat")
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as stream:
            return stream.read()

    def run_with_crash(self, at_state=None, nth_set_state=None):
        """Simulate a kill: die mid-transaction with no cleanup at all.

        The in-process `except BaseException: rollback()` in commit() is
        suppressed on purpose. A real SIGKILL or power cut does not get to run
        it, so the journal must survive on disk and the NEXT run has to be the
        thing that recovers. Testing the in-process path would be testing the
        easy case.
        """
        original = eapx.Transaction.set_state
        original_rollback = eapx.rollback
        counter = {"n": 0}

        class Boom(BaseException):
            pass

        def patched(self, record, state):
            original(self, record, state)
            counter["n"] += 1
            hit = (at_state is not None and state == at_state) or (
                nth_set_state is not None and counter["n"] == nth_set_state
            )
            if hit:
                raise Boom("simulated kill at %s" % state)

        eapx.Transaction.set_state = patched
        eapx.rollback = lambda *args, **kwargs: None
        try:
            self.install()
        except BaseException as error:
            if not isinstance(error, Boom):
                raise
        finally:
            eapx.Transaction.set_state = original
            eapx.rollback = original_rollback
        self.assertTrue(
            os.path.isfile(os.path.join(self.game, ".eapx", "test-port", "journal")),
            "the journal must survive a kill so the next run can recover",
        )

    def test_crash_at_each_commit_step_converges(self):
        for state in (eapx.BACKING_UP, eapx.BACKED_UP,
                      eapx.INSTALLING, eapx.INSTALLED):
            with self.subTest(state=state):
                self.setUp()
                self.prepare_reinstall()
                self.run_with_crash(state)
                level = self.read_level()
                self.assertIn(
                    level, (b"level data", b"level data v2"),
                    "tree left in a mixed state after a crash at %s" % state,
                )
                # A re-run must reach a good installation.
                self.assertEqual(self.install(), 0,
                                 "recovery failed after a crash at %s" % state)
                self.assertEqual(self.read_level(), b"level data v2")

    def test_rollback_is_idempotent(self):
        """Kill the recovery itself, halfway, then run it again.

        This is the one that used to destroy data: re-running a rollback that
        had already restored a path would delete what it restored, because the
        decision was 'does something exist here' rather than 'what is this'.
        """
        self.prepare_reinstall()
        self.run_with_crash(eapx.INSTALLED)

        # Second run: recovery starts, undoes the first path, and is killed.
        original = eapx.Transaction.set_state
        counter = {"n": 0}

        class Boom(BaseException):
            pass

        def patched(self, record, state):
            original(self, record, state)
            counter["n"] += 1
            raise Boom("killed during rollback")

        eapx.Transaction.set_state = patched
        try:
            self.install()
        except BaseException as error:
            if not isinstance(error, Boom):
                raise
        finally:
            eapx.Transaction.set_state = original
        self.assertEqual(counter["n"], 1, "the rollback did not start")

        # Third run: recovery must finish the job without destroying anything.
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.read_level(), b"level data v2")
        self.assert_installed()

    def test_externally_modified_path_is_not_touched(self):
        self.prepare_reinstall()
        self.run_with_crash(eapx.BACKED_UP)
        # Something else writes over the live tree while the transaction is open.
        stray = os.path.join(self.game, "lib", "arm64-v8a")
        os.makedirs(stray, exist_ok=True)
        with open(os.path.join(stray, "libgame.so"), "wb") as stream:
            stream.write(b"not what either side expected")
        code = self.install()
        self.assertEqual(code, 1, "recovery must refuse to guess")
        with open(os.path.join(stray, "libgame.so"), "rb") as stream:
            self.assertEqual(stream.read(), b"not what either side expected")


class HookTests(Base):
    def test_hook_timeout_kills_the_hook(self):
        recipe = self.recipe()
        recipe["hooks"] = [{
            "id": "hangs",
            "argv": ["/bin/sh", "-c", "sleep 30"],
            "timeout_seconds": 1,
        }]
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        start = __import__("time").time()
        self.assertEqual(self.install(), 1)
        self.assertLess(__import__("time").time() - start, 20,
                        "the hook was not killed by its timeout")

    def test_hook_runs_and_can_modify_the_stage(self):
        recipe = self.recipe()
        recipe["hooks"] = [{
            "id": "bake",
            "argv": ["/bin/sh", "-c", 'echo baked > "$EAPX_STAGE/assets/baked.dat"'],
        }]
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.game, "assets/baked.dat")))


class SafetyTests(Base):
    def test_zip_slip_is_rejected(self):
        self.write_recipe()
        entries = self.apk_entries()
        make_zip(os.path.join(self.data, "game.apk"), entries)
        # Append a traversal entry after the fact so zipfile does not normalise it.
        with zipfile.ZipFile(os.path.join(self.data, "game.apk"), "a") as archive:
            archive.writestr("../../escape.txt", b"nope")
        self.assertEqual(self.install(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.txt")))

    def test_digest_cache_reads_each_byte_once(self):
        cache = eapx.DigestCache()
        path = os.path.join(self.root, "blob.bin")
        with open(path, "wb") as stream:
            stream.write(b"x" * 4096)
        first = cache.file(path)
        after_first = cache.bytes_read
        second = cache.file(path)
        self.assertEqual(first, second)
        self.assertEqual(cache.bytes_read, after_first, "the file was read twice")


class PortMasterTests(Base):
    """Integration with the runtime PortMaster already ships."""

    def setUp(self):
        Base.setUp(self)
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        Base.tearDown(self)

    def test_tty_progress_uses_the_eapx_brand(self):
        import io
        self.assertEqual(eapx.EAPX_LOGO, (
            "▄▖▄▖▄▖▖▖",
            "▙▖▌▌▙▌▚▘",
            "▙▖▛▌▌ ▌▌",
        ))
        self.assertEqual(eapx.EAPRULES_SIGNATURE, (
            "█▀▄ █ █   █▀▀ █▀█ █▀█ █▀▄ █ █ █   █▀▀ █▀▀",
            "█▀▄  █    █▀▀ █▀█ █▀▀ █▀▄ █ █ █   █▀▀ ▀▀█",
            "▀▀   ▀    ▀▀▀ ▀ ▀ ▀   ▀ ▀ ▀▀▀ ▀▀▀ ▀▀▀ ▀▀▀",
        ))
        screen = io.StringIO()
        progress = eapx.Progress(
            None, eapx.Logger(None, verbose=False), title="Synthetic Game",
            tty="none", portmaster=None,
        )
        progress.tty = screen
        progress.update(
            overall=560, message="EXTRACTING GAME DATA",
            detail="chapter.dat", force=True,
        )
        for line in eapx.EAPX_LOGO + eapx.EAPRULES_SIGNATURE:
            self.assertIn(line, screen.getvalue())
        self.assertIn("Android Port eXtractor · v%s" % eapx.VERSION,
                      screen.getvalue())
        self.assertIn("IMPORTING", screen.getvalue())
        self.assertIn("Synthetic Game", screen.getvalue())

    def test_tty_version_is_read_from_the_engine_constant(self):
        import io
        original = eapx.VERSION
        screen = io.StringIO()
        progress = eapx.Progress(
            None, eapx.Logger(None, verbose=False), title="Synthetic Game",
            tty="none", portmaster=None,
        )
        progress.tty = screen
        try:
            eapx.VERSION = "9.8.7"
            progress.update(overall=1, message="LOOKING", force=True)
        finally:
            eapx.VERSION = original
        self.assertIn("Android Port eXtractor · v9.8.7", screen.getvalue())

    def test_tty_updates_in_place_without_scrolling(self):
        class RecordingScreen:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def fileno(self):
                raise OSError("not a real tty")

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

        screen = RecordingScreen()
        progress = eapx.Progress(
            None, eapx.Logger(None, verbose=False), title="Synthetic Game",
            tty="none", portmaster=None,
        )
        progress.tty = screen
        progress.update(overall=100, message="READING", force=True)
        progress.update(overall=200, message="EXTRACTING", force=True)

        self.assertEqual(len(screen.writes), 2)
        self.assertIn("\033[2J", screen.writes[0])
        self.assertNotIn("\033[2J", screen.writes[1])
        self.assertTrue(screen.writes[1].startswith("\033[?25l"))
        for write in screen.writes:
            self.assertFalse(write.endswith("\n"))
            self.assertNotIn("\n", write)
            positions = [
                (int(row), int(column))
                for row, column in re.findall(r"\033\[(\d+);(\d+)H", write)
            ]
            self.assertTrue(positions)
            self.assertTrue(all(row < 24 for row, _column in positions))
            self.assertIn(eapx.EAPRULES_SIGNATURE[-1], write)

    def test_tty_centres_content_inside_a_safe_area(self):
        class RecordingScreen:
            encoding = "utf-8"

            def __init__(self):
                self.value = ""

            def fileno(self):
                return 123

            def write(self, value):
                self.value += value

            def flush(self):
                pass

        screen = RecordingScreen()
        progress = eapx.Progress(
            None, eapx.Logger(None, verbose=False), title="Synthetic Game",
            tty="none", portmaster=None,
        )
        progress.tty = screen
        terminal_size = os.terminal_size((48, 16))
        with mock.patch.object(eapx.os, "get_terminal_size",
                               return_value=terminal_size):
            progress.update(overall=560, message="EXTRACTING", force=True)

        writes = re.findall(r"\033\[(\d+);(\d+)H([^\033]*)", screen.value)
        visible = [
            (int(row), int(column), value)
            for row, column, value in writes if value
        ]
        self.assertTrue(visible)
        for row, column, value in visible:
            self.assertGreaterEqual(row, 2)
            self.assertLessEqual(row, 15)
            self.assertGreaterEqual(column, 3)
            self.assertLessEqual(column + len(value) - 1, 46)
        first = min(row for row, _column, _value in visible)
        last = max(row for row, _column, _value in visible)
        self.assertGreater(first, 1)
        self.assertLess(last, 16)
        signature = next(
            (row, column, value) for row, column, value in visible
            if value == eapx.EAPRULES_SIGNATURE[-1]
        )
        self.assertEqual(signature[1],
                         3 + (44 - len(signature[2])) // 2)

    def test_tty_clears_again_when_the_terminal_geometry_changes(self):
        class RecordingScreen:
            encoding = "utf-8"

            def __init__(self):
                self.writes = []

            def fileno(self):
                return 123

            def write(self, value):
                self.writes.append(value)

            def flush(self):
                pass

        screen = RecordingScreen()
        progress = eapx.Progress(
            None, eapx.Logger(None, verbose=False), title="Synthetic Game",
            tty="none", portmaster=None,
        )
        progress.tty = screen
        with mock.patch.object(
                eapx.os, "get_terminal_size",
                side_effect=(os.terminal_size((80, 24)),
                             os.terminal_size((64, 20)))):
            progress.update(overall=100, message="READING", force=True)
            progress.update(overall=200, message="EXTRACTING", force=True)
        self.assertEqual(len(screen.writes), 2)
        self.assertIn("\033[2J", screen.writes[0])
        self.assertIn("\033[2J", screen.writes[1])

    def test_tty_short_layout_keeps_the_progress_bar_in_bounds(self):
        class RecordingScreen:
            encoding = "utf-8"

            def __init__(self):
                self.value = ""

            def fileno(self):
                return 123

            def write(self, value):
                self.value += value

            def flush(self):
                pass

        screen = RecordingScreen()
        progress = eapx.Progress(
            None, eapx.Logger(None, verbose=False), title="Synthetic Game",
            tty="none", portmaster=None,
        )
        progress.tty = screen
        with mock.patch.object(
                eapx.os, "get_terminal_size",
                return_value=os.terminal_size((80, 10))):
            progress.update(overall=560, message="EXTRACTING", force=True)
        self.assertIn(" 56%", screen.value)
        positions = [
            int(row) for row in re.findall(r"\033\[(\d+);\d+H", screen.value)
        ]
        self.assertTrue(positions)
        self.assertTrue(all(row < 10 for row in positions))

    def test_tty_progress_has_an_ascii_fallback(self):
        class AsciiScreen:
            encoding = "ascii"

            def __init__(self):
                self.value = ""

            def fileno(self):
                raise OSError("not a real tty")

            def write(self, value):
                self.value += value

            def flush(self):
                pass

        screen = AsciiScreen()
        progress = eapx.Progress(
            None, eapx.Logger(None, verbose=False), title="Synthetic Game",
            tty="none", portmaster=None,
        )
        progress.tty = screen
        progress.update(overall=560, message="EXTRACTING", force=True)
        self.assertIn("EAPX", screen.value)
        self.assertIn("BY EAPRULES", screen.value)
        self.assertTrue(all(ord(character) < 128 for character in screen.value))

    def test_placeholder_is_removed_once_the_game_is_in(self):
        recipe = self.recipe()
        recipe["placeholder"] = "gamedata/place NeededFiles here"
        self.write_recipe(recipe)
        marker = os.path.join(self.data, "place NeededFiles here")
        open(marker, "w").close()
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        self.assertFalse(os.path.exists(marker), "the placeholder survived")

    def test_placeholder_survives_a_failed_install(self):
        recipe = self.recipe()
        recipe["placeholder"] = "gamedata/place NeededFiles here"
        self.write_recipe(recipe)
        marker = os.path.join(self.data, "place NeededFiles here")
        open(marker, "w").close()
        self.assertEqual(self.install(), 1)
        self.assertTrue(os.path.exists(marker),
                        "the instruction vanished while the game is still missing")

    def test_patcher_protocol_only_speaks_when_the_patcher_is_listening(self):
        import contextlib, io
        self.write_recipe()
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        os.environ.pop("PATCHER_GAME", None)
        os.environ.pop("PATCHER_FILE", None)
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            self.assertEqual(self.install(), 0)
        self.assertNotIn("Patching completed", quiet.getvalue())

    def test_patcher_protocol_reports_success(self):
        import contextlib, io
        self.write_recipe()
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        os.environ["PATCHER_GAME"] = "Test Port"
        spoken = io.StringIO()
        with contextlib.redirect_stdout(spoken):
            self.assertEqual(self.install(), 0)
        self.assertIn("Patching completed successfully!", spoken.getvalue())

    def test_patcher_protocol_reports_failure(self):
        import contextlib, io
        self.write_recipe()
        os.environ["PATCHER_GAME"] = "Test Port"
        spoken = io.StringIO()
        with contextlib.redirect_stdout(spoken):
            self.assertEqual(self.install(), 1)
        self.assertIn("Patching process failed!", spoken.getvalue())


class AdoptionTests(Base):
    """Accepting an install that lost its marker, without the package."""

    def recipe(self, **overrides):
        base = Base.recipe(self, **overrides)
        base.setdefault("validate", [
            {"path": "lib/{abi}/libgame.so", "elf_machine": "{abi}"},
            {"path": "assets", "min_files": 2},
        ])
        return base

    def install_then_lose_the_marker(self):
        self.write_recipe()
        source = os.path.join(self.data, "game.apk")
        make_zip(source, self.apk_entries())
        self.assertEqual(self.install(), 0)
        os.unlink(os.path.join(self.game, ".eapx-test-port.json"))
        os.unlink(source)

    def test_adopts_an_install_that_lost_its_marker(self):
        self.install_then_lose_the_marker()
        self.assertEqual(self.install(), 0, "should adopt instead of demanding the APK")
        self.assert_installed()
        with open(os.path.join(self.game, ".eapx-test-port.json")) as stream:
            marker = json.load(stream)
        self.assertTrue(marker["adopted"])
        self.assertEqual(len(marker["items"]), 3)

    def test_the_adopted_marker_drives_the_fast_path(self):
        self.install_then_lose_the_marker()
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.install(), 0)

    def test_refuses_when_a_commit_root_is_missing(self):
        self.install_then_lose_the_marker()
        shutil.rmtree(os.path.join(self.game, "assets"))
        self.assertEqual(self.install(), 1, "adopted a half-installed tree")

    def test_refuses_when_validation_fails(self):
        self.install_then_lose_the_marker()
        os.unlink(os.path.join(self.game, "assets", "texture.bin"))
        self.assertEqual(self.install(), 1, "adopted a tree failing min_files")

    def test_refuses_a_recipe_with_nothing_to_judge_by(self):
        """No validate block means adoption would accept anything at all."""
        recipe = Base.recipe(self)
        self.write_recipe(recipe)
        source = os.path.join(self.data, "game.apk")
        make_zip(source, self.apk_entries())
        self.assertEqual(self.install(), 0)
        os.unlink(os.path.join(self.game, ".eapx-test-port.json"))
        os.unlink(source)
        self.assertEqual(self.install(), 1)

    def test_no_adopt_flag_disables_it(self):
        self.install_then_lose_the_marker()
        self.assertEqual(self.install("--no-adopt"), 1)

    def test_verify_admits_content_was_not_checked(self):
        import contextlib, io
        self.install_then_lose_the_marker()
        self.assertEqual(self.install(), 0)
        spoken = io.StringIO()
        with contextlib.redirect_stdout(spoken):
            eapx.main(["verify", "--recipe", self.recipe_path,
                       "--game-dir", self.game, "--quiet"])
        self.assertIn("content unverified", spoken.getvalue())


class ProfileTests(Base):
    def digest(self, payload):
        return hashlib.sha256(payload).hexdigest()

    def profiled_recipe(self):
        recipe = self.recipe()
        recipe["validate"] = [
            {"path": "lib/{abi}/libgame.so", "elf_machine": "{abi}"},
            {"path": "assets", "min_files": 2},
        ]
        recipe["profiles"] = [
            {
                "id": "alpha",
                "validate": [
                    {"path": "assets/level.dat",
                     "sha256": self.digest(b"level data")},
                    {"path": "assets/texture.bin",
                     "sha256": self.digest(b"texture data")},
                ],
            },
            {
                "id": "beta",
                "validate": [
                    {"path": "assets/level.dat",
                     "sha256": self.digest(b"other level")},
                    {"path": "assets/texture.bin",
                     "sha256": self.digest(b"other texture")},
                ],
            },
        ]
        return recipe

    def marker_path(self):
        return os.path.join(self.game, ".eapx-test-port.json")

    def test_single_coherent_profile_is_recorded(self):
        self.write_recipe(self.profiled_recipe())
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        with open(self.marker_path()) as stream:
            marker = json.load(stream)
        self.assertEqual(marker["donor_profile"], "alpha")

    def test_unknown_profile_is_rejected_before_commit(self):
        self.write_recipe(self.profiled_recipe())
        entries = self.apk_entries()
        entries["assets/level.dat"] = b"unknown"
        entries["assets/texture.bin"] = b"unknown"
        make_zip(os.path.join(self.data, "game.apk"), entries)
        self.assertEqual(self.install(), 1)
        self.assertFalse(os.path.exists(self.marker_path()))
        self.assertFalse(os.path.exists(os.path.join(self.game, "assets")))

    def test_mixed_profile_is_rejected_before_commit(self):
        self.write_recipe(self.profiled_recipe())
        entries = self.apk_entries()
        entries["assets/texture.bin"] = b"other texture"
        make_zip(os.path.join(self.data, "game.apk"), entries)
        self.assertEqual(self.install(), 1)
        self.assertFalse(os.path.exists(self.marker_path()))

    def test_ambiguous_profiles_are_rejected(self):
        recipe = self.profiled_recipe()
        duplicate = dict(recipe["profiles"][0])
        duplicate["id"] = "also-alpha"
        recipe["profiles"].append(duplicate)
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        self.assertEqual(self.install(), 1)
        self.assertFalse(os.path.exists(self.marker_path()))

    def test_verify_rejects_profile_changed_from_marker(self):
        import contextlib, io
        self.write_recipe(self.profiled_recipe())
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        with open(self.marker_path()) as stream:
            marker = json.load(stream)
        marker["donor_profile"] = "beta"
        with open(self.marker_path(), "w") as stream:
            json.dump(marker, stream)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = eapx.main(["verify", "--recipe", self.recipe_path,
                                "--game-dir", self.game, "--quiet"])
        self.assertEqual(result, 1)
        self.assertIn("donor profile changed", errors.getvalue())

    def test_old_marker_without_profile_is_upgraded(self):
        self.write_recipe(self.profiled_recipe())
        source = os.path.join(self.data, "game.apk")
        make_zip(source, self.apk_entries())
        self.assertEqual(self.install(), 0)
        with open(self.marker_path()) as stream:
            marker = json.load(stream)
        del marker["donor_profile"]
        with open(self.marker_path(), "w") as stream:
            json.dump(marker, stream)
        os.unlink(source)
        self.assertEqual(self.install(), 0)
        with open(self.marker_path()) as stream:
            upgraded = json.load(stream)
        self.assertEqual(upgraded["donor_profile"], "alpha")


class CriticalRegionTests(Base):
    def regions(self):
        # Deliberately not sorted by offset: concatenation follows recipe order.
        return [
            {"offset": 64, "size": 8},
            {"offset": "0x0", "size": 20},
        ]

    def region_digest(self, payload, regions=None):
        digest = hashlib.sha256()
        for region in regions or self.regions():
            offset = region["offset"]
            if isinstance(offset, str):
                offset = int(offset, 16)
            digest.update(payload[offset:offset + region["size"]])
        return digest.hexdigest()

    def critical_recipe(self, known_payload=ARM64_SO, region_payload=ARM64_SO,
                        include_full_hash=True):
        recipe = self.recipe(requires_eapx=">=0.3.0")
        validation = {
            "size": len(known_payload),
            "elf_machine": "{abi}",
            "critical_regions": {
                "regions": self.regions(),
                "sha256": self.region_digest(region_payload),
            },
        }
        if include_full_hash:
            validation["sha256"] = hashlib.sha256(known_payload).hexdigest()
        recipe["extract"][0]["validate"] = validation
        recipe["validate"] = [{
            "path": "lib/{abi}/libgame.so",
            **validation
        }]
        return recipe

    def entries_with_library(self, payload):
        entries = self.apk_entries()
        entries["lib/arm64-v8a/libgame.so"] = payload
        return entries

    def test_known_full_hash_accepts_without_region_fallback(self):
        recipe = self.critical_recipe(region_payload=b"x" * len(ARM64_SO))
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "known.apk"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        with open(os.path.join(self.game, "eapx.log")) as stream:
            self.assertNotIn("accepted by critical regions", stream.read())

    def test_unknown_full_hash_with_matching_regions_is_accepted(self):
        variant = bytearray(ARM64_SO)
        variant[-1] ^= 0x01
        variant = bytes(variant)
        self.write_recipe(self.critical_recipe())
        make_zip(os.path.join(self.data, "variant.apk"),
                 self.entries_with_library(variant))
        self.assertEqual(self.install(), 0)
        with open(os.path.join(self.game, "eapx.log")) as stream:
            log = stream.read()
        self.assertIn("accepted lib/arm64-v8a/libgame.so by critical regions", log)
        self.assertIn(hashlib.sha256(variant).hexdigest(), log)
        with open(os.path.join(self.game, ".eapx-test-port.json")) as stream:
            marker = json.load(stream)
        native = next(item for item in marker["items"]
                      if item["rule"] == "native-library")
        self.assertEqual(native["sha256"], hashlib.sha256(variant).hexdigest())

    def test_output_check_alone_logs_region_fallback(self):
        variant = bytearray(ARM64_SO)
        variant[-1] ^= 0x01
        variant = bytes(variant)
        recipe = self.critical_recipe()
        recipe["extract"][0]["validate"] = {
            "size": len(ARM64_SO),
            "elf_machine": "{abi}",
        }
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "variant.apk"),
                 self.entries_with_library(variant))
        self.assertEqual(self.install(), 0)
        with open(os.path.join(self.game, "eapx.log")) as stream:
            log = stream.read()
        self.assertIn("accepted lib/arm64-v8a/libgame.so by critical regions", log)
        self.assertIn(hashlib.sha256(variant).hexdigest(), log)

    def test_critical_regions_work_without_a_full_hash_allowlist(self):
        variant = bytearray(ARM64_SO)
        variant[-1] ^= 0x01
        self.write_recipe(self.critical_recipe(include_full_hash=False))
        make_zip(os.path.join(self.data, "variant.apk"),
                 self.entries_with_library(bytes(variant)))
        self.assertEqual(self.install(), 0)

    def test_unknown_donor_with_changed_critical_region_is_rejected(self):
        variant = bytearray(ARM64_SO)
        variant[65] ^= 0x01
        self.write_recipe(self.critical_recipe())
        make_zip(os.path.join(self.data, "incompatible.apk"),
                 self.entries_with_library(bytes(variant)))
        self.assertEqual(self.install(), 1)
        self.assertFalse(os.path.exists(
            os.path.join(self.game, ".eapx-test-port.json")
        ))

    def test_blob_source_uses_the_same_region_fallback(self):
        known = b"critical" + b"A" * 32
        variant = b"critical" + b"B" * 32
        regions = [{"offset": 0, "size": 8}]
        recipe = {
            "schema": 1,
            "id": "critical-blob",
            "version": "1",
            "requires_eapx": ">=0.3.0",
            "extract": [{
                "id": "blob",
                "destination": "payload.bin",
                "source": {"kind": "blob", "patterns": ["donor.bin"]},
                "validate": {
                    "size": len(known),
                    "sha256": hashlib.sha256(known).hexdigest(),
                    "critical_regions": {
                        "regions": regions,
                        "sha256": self.region_digest(variant, regions),
                    },
                },
            }],
            "commit": ["payload.bin"],
        }
        self.write_recipe(recipe)
        with open(os.path.join(self.data, "donor.bin"), "wb") as stream:
            stream.write(variant)
        self.assertEqual(self.install(), 0)
        with open(os.path.join(self.game, "payload.bin"), "rb") as stream:
            self.assertEqual(stream.read(), variant)

    def test_wrong_size_is_rejected_before_content_hashing(self):
        from unittest import mock
        recipe = self.critical_recipe()
        validation = recipe["extract"][0]["validate"]
        validation["size"] += 1
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "wrong-size.apk"), self.apk_entries())
        with mock.patch.object(
            eapx.DigestCache, "member",
            side_effect=AssertionError("content was hashed before size rejection"),
        ):
            self.assertEqual(self.install(), 1)

    def test_recipe_without_regions_keeps_full_hash_behavior(self):
        variant = bytearray(ARM64_SO)
        variant[-1] ^= 0x01
        recipe = self.recipe()
        recipe["extract"][0]["validate"]["sha256"] = hashlib.sha256(
            ARM64_SO
        ).hexdigest()
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "variant.apk"),
                 self.entries_with_library(bytes(variant)))
        self.assertEqual(self.install(), 1)

    def test_regions_are_normalised_and_bounded_at_recipe_load(self):
        self.write_recipe(self.critical_recipe())
        parsed = eapx.Recipe(self.recipe_path)
        regions = parsed.rules[0]["validate"]["critical_regions"]["regions"]
        self.assertEqual(regions[1]["offset"], 0)

        recipe = self.critical_recipe()
        recipe["extract"][0]["validate"]["critical_regions"]["regions"] = [
            {"offset": len(ARM64_SO) - 1, "size": 2}
        ]
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError) as caught:
            eapx.Recipe(self.recipe_path)
        self.assertIn("extends past", str(caught.exception))

    def test_critical_regions_require_exact_size(self):
        recipe = self.critical_recipe()
        del recipe["extract"][0]["validate"]["size"]
        self.write_recipe(recipe)
        with self.assertRaises(eapx.RecipeError) as caught:
            eapx.Recipe(self.recipe_path)
        self.assertIn("requires an exact size", str(caught.exception))

    def test_invalid_region_values_are_recipe_errors(self):
        cases = [
            ({"offset": True, "size": 1}, "offset"),
            ({"offset": "100", "size": 1}, "offset"),
            ({"offset": "0X10", "size": 1}, "offset"),
            ({"offset": 0, "size": 0}, "positive"),
            ({"offset": 0, "size": True}, "positive"),
        ]
        for region, message in cases:
            recipe = self.critical_recipe()
            recipe["extract"][0]["validate"]["critical_regions"]["regions"] = [
                region
            ]
            self.write_recipe(recipe)
            with self.assertRaises(eapx.RecipeError, msg=repr(region)) as caught:
                eapx.Recipe(self.recipe_path)
            self.assertIn(message, str(caught.exception))


class VersionRequirementTests(Base):
    def test_compatible_requirement_is_accepted(self):
        recipe = self.recipe(requires_eapx=">=0.2.0")
        self.write_recipe(recipe)
        parsed = eapx.Recipe(self.recipe_path)
        self.assertEqual(parsed.requires_eapx, ">=0.2.0")

    def test_invalid_requirements_are_rejected(self):
        for value in ("0.2.0", ">0.2.0", ">=0.2", ">=v0.2.0", 2):
            self.write_recipe(self.recipe(requires_eapx=value))
            with self.assertRaises(eapx.RecipeError, msg=repr(value)):
                eapx.Recipe(self.recipe_path)

    def test_incompatible_requirement_is_rejected_before_discovery(self):
        self.write_recipe(self.recipe(requires_eapx=">=9.0.0"))
        self.assertEqual(self.install(), 1)
        self.assertFalse(os.path.exists(os.path.join(self.game, ".eapx")))


class LegacyCompatibilityTests(Base):
    def test_profileless_recipe_keeps_the_legacy_marker_and_verify_output(self):
        import contextlib, io
        recipe = self.recipe()
        self.assertNotIn("profiles", recipe)
        self.assertNotIn("requires_eapx", recipe)
        self.write_recipe(recipe)
        make_zip(os.path.join(self.data, "game.apk"), self.apk_entries())
        self.assertEqual(self.install(), 0)
        marker_path = os.path.join(self.game, ".eapx-test-port.json")
        with open(marker_path) as stream:
            marker = json.load(stream)
        self.assertNotIn("donor_profile", marker)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = eapx.main(["verify", "--recipe", self.recipe_path,
                                "--game-dir", self.game, "--quiet"])
        self.assertEqual(result, 0)
        self.assertNotIn("profile=", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
