#!/usr/bin/env python3
"""Run eapx against external recipes and donors without writing to them."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile


class ExternalMutation(Exception):
    pass


class SourceGuard:
    def __init__(self, paths):
        self.paths = paths
        self.before = {path: snapshot(path) for path in paths}

    def __enter__(self):
        return self

    def __exit__(self, _kind, _value, _traceback):
        after = {path: snapshot(path) for path in self.paths}
        if self.before != after:
            raise ExternalMutation(
                "an external recipe or donor changed during the smoke test"
            )


def snapshot(path):
    result = []
    if os.path.isdir(path):
        for base, directories, files in os.walk(path):
            directories.sort()
            for name in sorted(files):
                item = os.path.join(base, name)
                info = os.lstat(item)
                result.append((os.path.relpath(item, path), info.st_size,
                               info.st_mtime_ns, info.st_mode))
    else:
        info = os.lstat(path)
        result.append((os.path.basename(path), info.st_size,
                       info.st_mtime_ns, info.st_mode))
    return result


def copy_input(source, destination):
    if os.path.isdir(source):
        shutil.copytree(source, destination)
    else:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)


def run(argv):
    completed = subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, universal_newlines=True,
    )
    return completed


def fail(message, completed=None):
    sys.stderr.write("error: %s\n" % message)
    if completed is not None:
        if completed.stdout:
            sys.stderr.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copy external donors into mktemp and exercise eapx"
    )
    parser.add_argument("--engine", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "eapx.py"
    ))
    parser.add_argument("--recipe", required=True)
    parser.add_argument("--donor", action="append", required=True)
    parser.add_argument("--expected-profile")
    parser.add_argument("--expect-failure", choices=("plan", "install"))
    args = parser.parse_args(argv)

    engine = os.path.realpath(args.engine)
    recipe_source = os.path.realpath(args.recipe)
    donor_sources = [os.path.realpath(path) for path in args.donor]
    sources = [recipe_source] + donor_sources
    for path in [engine] + sources:
        if not os.path.exists(path):
            return fail("path does not exist: %s" % path)
    with SourceGuard(sources), tempfile.TemporaryDirectory(
        prefix="eapx-external-"
    ) as temporary:
        recipe = os.path.join(temporary, "recipe.json")
        shutil.copy2(recipe_source, recipe)
        game_dir = os.path.join(temporary, "game")
        input_dir = os.path.join(game_dir, "gamedata")
        os.makedirs(input_dir)
        copied = []
        for index, source in enumerate(donor_sources):
            name = "%02d-%s" % (index, os.path.basename(source.rstrip(os.sep)))
            destination = os.path.join(input_dir, name)
            copy_input(source, destination)
            copied.append(destination)

        common = ["--recipe", recipe, "--game-dir", game_dir, "--quiet"]
        explicit = []
        for path in copied:
            explicit.extend(["--input", path])

        planned = run([sys.executable, engine, "plan"] + common + explicit)
        if args.expect_failure == "plan":
            if planned.returncode == 0:
                return fail("plan unexpectedly succeeded", planned)
            sys.stdout.write("ok: plan rejected the donor set before installation\n")
            return 0
        if planned.returncode != 0:
            return fail("plan failed", planned)
        try:
            plan = json.loads(planned.stdout)
        except ValueError:
            return fail("plan did not emit JSON", planned)

        installed = run(
            [sys.executable, engine, "install"] + common + explicit
            + ["--no-adopt", "--no-portmaster", "--tty", "none"]
        )
        if args.expect_failure == "install":
            if installed.returncode == 0:
                return fail("install unexpectedly succeeded", installed)
            sys.stdout.write("ok: install rejected the donor before commit\n")
            return 0
        if installed.returncode != 0:
            return fail("install failed", installed)

        with open(recipe, "r", encoding="utf-8") as stream:
            recipe_data = json.load(stream)
        marker_name = recipe_data.get(
            "marker", ".eapx-%s.json" % recipe_data["id"]
        )
        marker_path = os.path.join(game_dir, marker_name)
        try:
            with open(marker_path, "r", encoding="utf-8") as stream:
                marker = json.load(stream)
        except (OSError, ValueError) as error:
            return fail("cannot read marker: %s" % error)
        if args.expected_profile is not None:
            if marker.get("donor_profile") != args.expected_profile:
                return fail(
                    "profile %r, expected %r"
                    % (marker.get("donor_profile"), args.expected_profile)
                )

        verified = run([sys.executable, engine, "verify"] + common)
        if verified.returncode != 0:
            return fail("verify failed", verified)

        shutil.rmtree(input_dir)
        second = run(
            [sys.executable, engine, "install"] + common + explicit
            + ["--no-portmaster", "--tty", "none"]
        )
        if second.returncode != 0:
            return fail("second install did not use the marker fast path", second)

        sys.stdout.write(json.dumps({
            "abi": plan["abi"],
            "items": len(plan["items"]),
            "profile": marker.get("donor_profile"),
            "fast_path": True,
            "verified": True,
        }, sort_keys=True) + "\n")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExternalMutation as error:
        sys.exit(fail(str(error)))
