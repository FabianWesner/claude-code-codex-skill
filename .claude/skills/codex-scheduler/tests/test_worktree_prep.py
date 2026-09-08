#!/usr/bin/env python3
"""Tests for `prepare_worktree` in scheduler_cli.

Run them all with:

    python3 .claude/skills/codex-scheduler/tests/run_tests.py

The rule under test, and the reason this file exists: `vendor` must end up as a REAL directory
inside the worktree, never a symlink. Composer resolves `$baseDir = dirname($vendorDir)` from the
real path when it writes `vendor/composer/autoload_psr4.php`, so a symlinked `vendor` makes a
worktree autoload the main checkout's `App\\` and `Tests\\` -- the lane's tests then pass while
exercising main's code. `node_modules` has no such problem and stays a symlink.
"""
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

import scheduler_cli  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


def make_repo(root):
    """A minimal git repo standing in for a Laravel checkout."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "test")
    with open(os.path.join(root, "composer.json"), "w") as f:
        f.write('{"autoload":{"psr-4":{"App\\\\":"app/"}}}\n')
    os.makedirs(os.path.join(root, "app"), exist_ok=True)
    with open(os.path.join(root, "app", "Thing.php"), "w") as f:
        f.write("<?php\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    # Untracked dependency trees, exactly as they exist in a real checkout.
    os.makedirs(os.path.join(root, "vendor", "composer"))
    with open(os.path.join(root, "vendor", "composer", "autoload_psr4.php"), "w") as f:
        f.write(f"<?php\n$baseDir = '{root}';\n")
    os.makedirs(os.path.join(root, "node_modules", "left-pad"))
    with open(os.path.join(root, "node_modules", "left-pad", "index.js"), "w") as f:
        f.write("module.exports = 1;\n")
    with open(os.path.join(root, ".env"), "w") as f:
        f.write("APP_ENV=local\nDB_DATABASE=/tmp/main.sqlite\n")


class PrepareWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self.tmp.name)
        make_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self, slug="lane1", symlinks=True):
        path, branch, notes = scheduler_cli.prepare_worktree(self.repo, slug, symlinks=symlinks)
        return path, branch, "\n".join(notes)

    def test_vendor_is_a_real_directory_and_node_modules_is_a_symlink(self):
        path, branch, notes = self.prepare()
        vendor = os.path.join(path, "vendor")
        node_modules = os.path.join(path, "node_modules")

        self.assertEqual(branch, "lane/lane1")
        self.assertTrue(os.path.isdir(vendor), "vendor should exist in the worktree")
        self.assertFalse(os.path.islink(vendor),
                         "vendor must be a real copy, not a symlink (Composer autoload paths)")
        self.assertTrue(
            os.path.isfile(os.path.join(vendor, "composer", "autoload_psr4.php")),
            "the cloned vendor should carry the main checkout's contents")
        self.assertTrue(os.path.islink(node_modules), "node_modules must stay a symlink")
        self.assertEqual(os.path.realpath(node_modules),
                         os.path.realpath(os.path.join(self.repo, "node_modules")))

    def test_vendor_copy_is_independent_of_the_main_checkout(self):
        path, _, _ = self.prepare()
        marker = os.path.join(path, "vendor", "lane-only.txt")
        with open(marker, "w") as f:
            f.write("x")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "vendor", "lane-only.txt")),
                         "writing in the worktree's vendor must not touch the main checkout")

    def test_notes_explain_the_vendor_copy(self):
        _, _, notes = self.prepare()
        self.assertIn("vendor copied by", notes)
        self.assertIn("not symlinked", notes)
        self.assertIn("symlinked node_modules", notes)

    def test_env_copy_behaviour_is_unchanged(self):
        path, _, notes = self.prepare()
        dst_env = os.path.join(path, ".env")
        self.assertTrue(os.path.isfile(dst_env))
        with open(dst_env) as f:
            body = f.read()
        self.assertIn("APP_ENV=local", body)
        self.assertIn(f"DB_DATABASE={os.path.join(path, 'database', 'database.sqlite')}", body)
        self.assertIn(".env copied", notes)

    def test_no_symlinks_skips_all_dependency_preparation(self):
        path, _, notes = self.prepare(slug="lane2", symlinks=False)
        self.assertIn("--no-symlinks", notes)
        for name in ("vendor", "node_modules", ".env"):
            self.assertFalse(os.path.exists(os.path.join(path, name)),
                             f"{name} must not be prepared under --no-symlinks")

    def test_preparation_is_idempotent_for_an_existing_worktree(self):
        first, _, _ = self.prepare()
        with open(os.path.join(first, "vendor", "keep.txt"), "w") as f:
            f.write("keep")
        second, _, notes = self.prepare()
        self.assertEqual(first, second)
        self.assertIn("worktree already existed", notes)
        self.assertTrue(os.path.isfile(os.path.join(second, "vendor", "keep.txt")),
                        "an existing vendor must not be re-cloned over")


class CloneTreeTest(unittest.TestCase):
    def test_clone_tree_copies_contents_and_reports_how(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            os.makedirs(os.path.join(src, "nested"))
            with open(os.path.join(src, "nested", "f.txt"), "w") as f:
                f.write("hello")
            dst = os.path.join(tmp, "dst")
            how = scheduler_cli._clone_tree(src, dst)
            self.assertIn(how, ("APFS clone", "reflink copy", "plain copy"))
            self.assertFalse(os.path.islink(dst))
            with open(os.path.join(dst, "nested", "f.txt")) as f:
                self.assertEqual(f.read(), "hello")


class ComposerDumpAutoloadTest(unittest.TestCase):
    def test_skipped_without_composer_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(scheduler_cli._composer_dump_autoload(tmp))


if __name__ == "__main__":
    unittest.main()
