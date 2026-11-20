"""End-to-end tests against small throwaway git repositories (stdlib unittest, no dependencies)."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "examples"))

from pr_archaeologist import blame, cache, classify, cli, docs, history, model, render_html, symbols  # noqa: E402
import make_demo_repo  # noqa: E402


def git(repo, *args, date="2021-01-01T00:00:00+00:00", author="Ada"):
    env = dict(os.environ, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL="a@example.com", GIT_AUTHOR_DATE=date,
               GIT_COMMITTER_NAME=author, GIT_COMMITTER_EMAIL="a@example.com", GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout.strip()


def write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


def commit(repo, message, **kw):
    git(repo, "add", "-A", **kw)
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, **kw)
    return git(repo, "rev-parse", "HEAD")


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify.classify("fix: handle empty cart", "")["kind"], "fix")
        self.assertEqual(classify.classify("Fix crash when cart is empty (#12)", "")["kind"], "fix")
        self.assertEqual(classify.classify('Revert "Add cache"', "This reverts commit abcdef1234.")["kind"], "revert")
        self.assertEqual(classify.classify("security: escape user input", "")["kind"], "security")
        self.assertEqual(classify.classify("Introduce event bus", "")["kind"], "design")
        self.assertEqual(classify.classify("feat(api): add export", "")["kind"], "feature")
        self.assertEqual(classify.classify("Bump version", "")["kind"], "other")

    def test_lessons_from_a_real_repository(self):
        """Misreadings found when the tool was first run on Reversibility Classes."""
        kind = lambda subject: classify.classify(subject, "")["kind"]
        # The word "security" or "spec" alone says nothing about what kind of change it is.
        self.assertEqual(kind("Release v0.4.1: governance, security policy, pinned supply chain"), "release")
        self.assertNotEqual(kind("Add security policy and CODEOWNERS"), "security")
        self.assertNotEqual(kind("Spec 0.3.0: session egress guard, version-scoped rules"), "test")
        self.assertNotEqual(kind("Clear errors for typos and bad files"), "docs")
        # Hardening after a review is a security change even without the word "fix".
        self.assertEqual(kind("Harden the session guard after a second red-team pass"), "security")
        self.assertEqual(kind("Close second-review findings: launch spoofing, dedupe"), "security")
        self.assertEqual(kind("Workflows: least-privilege tokens"), "security")

    def test_bullets_stay_separate(self):
        got = classify.classify("fix: scan", "- Never match a record by tool names alone\n- Parse npx launch commands because\n  wrappers hide the package\n")
        self.assertEqual(got["warn"], ["Never match a record by tool names alone"])
        self.assertEqual(got["why"], ["Parse npx launch commands because wrappers hide the package"])

    def test_refs_and_reasons(self):
        got = classify.classify(
            "fix: double charge on retry (#31)",
            "Root cause: a timeout does not mean the charge failed.\n\nDo not remove the header.\n\nFixes #30, see PAY-812\n\nSigned-off-by: A <a@b.c>",
        )
        self.assertEqual(got["prs"], [31])
        self.assertEqual(got["issues"], [30])
        self.assertEqual(got["tickets"], ["PAY-812"])
        self.assertTrue(any("Root cause" in w for w in got["why"]))
        self.assertTrue(any("Do not remove" in w for w in got["warn"]))
        self.assertFalse(any("Signed-off" in w for w in got["why"] + got["warn"]))


class ParserTest(unittest.TestCase):
    def test_parse_record_with_rename(self):
        record = (
            "abc\x1fdef\x1fAda\x1fa@b.c\x1f2021-01-01T00:00:00+00:00\x1fsubject\x1fbody\x1f\x00\n"
            ":100644 100644 111 222 R066\x00old name.py\x00new.py\x00:000000 100644 000 333 A\x00d.txt\x00"
            "1\t0\t\x00old name.py\x00new.py\x001\t0\td.txt\x00"
        )
        got = history.parse_record(record)
        self.assertEqual(got["sha"], "abc")
        self.assertEqual(got["parents"], ["def"])
        self.assertIn(["new.py", 1, 0, "R", "old name.py"], got["files"])
        self.assertIn(["d.txt", 1, 0, "A", None], got["files"])

    def test_symbol_from_hunk_context(self):
        self.assertEqual(history.symbol_from_context("def charge_with_retry(order, card):"), "charge_with_retry")
        self.assertEqual(history.symbol_from_context("func (r *Repo) Save(ctx context.Context) error {"), "Save")
        self.assertEqual(history.symbol_from_context("static int parse_header(char *buf)"), "parse_header")

    def test_blame_incremental(self):
        sha1, sha2 = "a" * 40, "b" * 40
        text = f"{sha1} 3 3 1\nauthor T\nsummary two\nfilename c.py\n{sha2} 1 1 2\nauthor T\nfilename a.py\n"
        self.assertEqual(blame.parse_incremental(text), [(3, 1, sha1), (1, 2, sha2)])


class SymbolsTest(unittest.TestCase):
    def test_python(self):
        src = "class A:\n    def m(self):\n        return 1\n\n\ndef top():\n    pass\n"
        names = {s["name"]: (s["start"], s["end"]) for s in symbols.extract("x.py", src)}
        self.assertEqual(names, {"A": (1, 3), "A.m": (2, 3), "top": (6, 7)})

    def test_brace_languages(self):
        go = "package x\n\nfunc (r *Repo) Save(a int) error {\n\tif a > 0 {\n\t\treturn nil\n\t}\n\treturn nil\n}\n"
        self.assertEqual([(s["name"], s["start"], s["end"]) for s in symbols.extract("x.go", go)], [("Save", 3, 8)])
        ts = "export class Cart {\n  total(items: Item[]): number {\n    return 1;\n  }\n}\n\nexport const add = (a, b) => {\n  return a + b;\n};\n"
        self.assertEqual({s["name"] for s in symbols.extract("x.ts", ts)}, {"Cart", "Cart.total", "add"})


class DocsTest(unittest.TestCase):
    def test_adr(self):
        text = "# 7. Use queues\n\nDate: 2020-05-01\n\n## Status\n\nAccepted\n\n## Context\n\nSync calls time out.\n\n## Decision\n\nUse a queue in `svc/worker.py`.\n\n## Consequences\n\nOrdering is not guaranteed.\n"
        doc = docs.parse("docs/adr/0007-use-queues.md", text)
        self.assertEqual((doc["kind"], doc["number"], doc["status"], doc["date"]), ("adr", "0007", "Accepted", "2020-05-01"))
        self.assertEqual(doc["title"], "Use queues")
        self.assertIn("svc/worker.py", doc["paths"])
        self.assertIn("Ordering", doc["consequences"])

    def test_agent_note_sections(self):
        text = "# Why `svc/a.py` exists\n\nText.\n\n## Invariants\n\n- Never fail open (abc1234).\n- Keys are stable\n  across retries (ADR-2).\n\n## Known traps\n\n- Caching the token broke retries (def5678).\n"
        doc = docs.parse("docs/archaeology/svc-a.py.md", text)
        self.assertEqual(doc["invariants"], ["Never fail open (abc1234).", "Keys are stable across retries (ADR-2)."])
        self.assertEqual(doc["traps"], ["Caching the token broke retries (def5678)."])
        self.assertIn("svc/a.py", doc["paths"])

    def test_plain_doc(self):
        self.assertEqual(docs.parse("README.md", "# Hello\n\nSee `src/app.py`.\n")["kind"], "doc")


class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = os.path.join(cls.tmp.name, "ledgerline")
        with contextlib.redirect_stdout(io.StringIO()):
            make_demo_repo.build(cls.repo)
        code, _out, cls.err = run_cli("dig", "--repo", cls.repo)
        assert code == 0, cls.err
        cls.state = cache.load(cls.repo)
        cls.report = model.build(cls.state, "ledgerline", "2024-06-05")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outputs_exist(self):
        self.assertIn("full run, 24 new commit(s)", self.err)
        for name in ("CODEBASE_ARCHAEOLOGY.md", "archaeology-graph.html"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), name)

    def test_rename_is_followed(self):
        ledger = self.report["files"]["payments/ledger.py"]
        self.assertEqual(ledger["previous"], ["payments/books.py"])
        self.assertEqual(ledger["first"], "2019-03-04")
        self.assertNotIn("payments/books.py", self.report["files"])

    def test_fragile_file_story(self):
        retry = self.report["files"]["payments/retry.py"]
        self.assertEqual(retry["level"], "fragile")
        self.assertEqual((retry["fixes"], retry["reverts"]), (5, 1))
        kinds = [p["kind"] for p in retry["pitfalls"]]
        for expected in ("fix", "reverted", "warning", "comment", "decision"):
            self.assertIn(expected, kinds)
        self.assertIn("ADR-0002", [d["label"] for d in retry["docs"]])
        self.assertEqual(retry["coupled"][0]["path"], "payments/gateway.py")

    def test_function_level_history(self):
        retry = self.report["files"]["payments/retry.py"]
        by_name = {s["name"]: s for s in retry["symbols"]}
        titles = [e["title"] for e in by_name["charge_with_retry"]["fixes"]]
        self.assertIn("off-by-one in retry budget", titles)
        # Rewritten lines no longer show in blame; hunk headers still remember the fix.
        self.assertIn("retry only on timeouts, not on declined cards", titles)
        self.assertIn("cap backoff at 30s so workers do not stall", [e["title"] for e in by_name["backoff"]["fixes"]])

    def test_deleted_file_is_remembered(self):
        soap = self.report["files"]["legacy/soap_client.py"]
        self.assertEqual(soap["status"], "deleted")
        self.assertIn("Remove legacy SOAP client", soap["deleted"]["title"])
        with open(os.path.join(self.repo, "CODEBASE_ARCHAEOLOGY.md"), encoding="utf-8") as fh:
            self.assertIn("## Graveyard", fh.read())

    def test_explain(self):
        code, out, _ = run_cli("explain", "payments/retry.py", "--repo", self.repo, "--no-color")
        self.assertEqual(code, 0)
        for needle in ("FRAGILE", "WHY IT EXISTS", "PAST PITFALLS", "Tried and reverted", "BEFORE YOU EDIT", "ADR-0002"):
            self.assertIn(needle, out)
        code, out, _ = run_cli("explain", "payments/books.py", "--repo", self.repo)  # old name still resolves
        self.assertEqual(code, 0)
        self.assertIn("payments/ledger.py", out)
        code, out, _ = run_cli("explain", "payments/retry.py", "--repo", self.repo, "--json")
        self.assertEqual(json.loads(out)["path"], "payments/retry.py")
        code, out, _ = run_cli("explain", "payments/retry.py", "--repo", self.repo, "-f", "backoff")
        self.assertIn("cap backoff at 30s", out)
        code, _out, err = run_cli("explain", "payments/retyr.py", "--repo", self.repo)
        self.assertEqual(code, 2)
        self.assertIn("payments/retry.py", err)
        code, out, _ = run_cli("explain", "payments", "--repo", self.repo)
        self.assertIn("payments/gateway.py", out)

    def test_html_is_self_contained_and_escaped(self):
        with open(os.path.join(self.repo, "archaeology-graph.html"), encoding="utf-8") as fh:
            page = fh.read()
        for banned in ("http://", "https://", "<link", "src="):
            self.assertNotIn(banned, page.replace("https://gateway.example", ""))
        payload = {"meta": {"repo": "x"}, "nodes": [{"title": "</script><script>alert(1)</script>"}]}
        self.assertNotIn("</script><script>", render_html.embed_json(payload))

    def test_second_run_is_a_noop(self):
        index = os.path.join(self.repo, "CODEBASE_ARCHAEOLOGY.md")
        before = os.stat(index).st_mtime_ns
        code, _out, err = run_cli("dig", "--repo", self.repo)
        self.assertEqual(code, 0)
        self.assertIn("noop run, 0 new commit(s)", err)
        self.assertIn("0 file(s) blamed", err)
        self.assertIn("nothing (outputs already up to date)", err)
        self.assertEqual(os.stat(index).st_mtime_ns, before)


class IncrementalTest(unittest.TestCase):
    def test_incremental_equals_full(self):
        """Analysing in two nightly steps must give exactly what one full run gives."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            git(repo, "init", "-q", "-b", "main")
            write(repo, "app/core.py", "def run():\n    return 1\n")
            write(repo, "app/util.py", "def helper():\n    return 2\n")
            commit(repo, "Initial import\n\nSo that we have something to run.", date="2020-01-01T00:00:00+00:00")
            write(repo, "app/core.py", "def run():\n    return helper()\n")
            first = commit(repo, "fix: run returned a constant\n\nBecause nobody called helper.", date="2020-02-01T00:00:00+00:00")

            code, _o, err = run_cli("dig", "--repo", repo)
            self.assertEqual(code, 0)
            self.assertIn("full run, 2 new commit(s)", err)

            git(repo, "mv", "app/util.py", "app/helpers.py")
            write(repo, "app/core.py", "def run():\n    return helper() + 1\n")
            commit(repo, "refactor: rename util to helpers", date="2020-03-01T00:00:00+00:00", author="Bob")
            write(repo, "app/core.py", "def run():\n    return helper()\n")
            commit(repo, f'Revert "refactor tweak"\n\nThis reverts commit {first}.\n\nIt broke staging.', date="2020-04-01T00:00:00+00:00")
            write(repo, "docs/adr/0001-helpers.md", "# 1. Helpers\n\n## Status\n\nAccepted\n\n## Decision\n\nKeep helpers in `app/helpers.py`.\n")
            commit(repo, "docs: ADR for helpers", date="2020-05-01T00:00:00+00:00")

            code, _o, err = run_cli("dig", "--repo", repo, "--json", "inc.json")
            self.assertEqual(code, 0)
            self.assertIn("incremental run, 3 new commit(s)", err)
            self.assertIn("2 file(s) blamed", err)  # core.py changed, helpers.py is new; nothing else re-blamed
            with open(os.path.join(repo, "CODEBASE_ARCHAEOLOGY.md"), encoding="utf-8") as fh:
                incremental_md = fh.read()
            with open(os.path.join(repo, "inc.json"), encoding="utf-8") as fh:
                incremental = json.load(fh)

            code, _o, err = run_cli("dig", "--repo", repo, "--full", "--json", "full.json")
            self.assertEqual(code, 0)
            self.assertIn("full run, 5 new commit(s)", err)
            with open(os.path.join(repo, "full.json"), encoding="utf-8") as fh:
                full = json.load(fh)
            with open(os.path.join(repo, "CODEBASE_ARCHAEOLOGY.md"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), incremental_md)
            self.assertEqual(full, incremental)
            self.assertEqual(full["files"]["app/helpers.py"]["previous"], ["app/util.py"])
            self.assertEqual(full["files"]["app/core.py"]["reverts"], 1)

    def test_docs_and_bystanders_are_not_fragile(self):
        """A README touched by every fix, and a version file bumped alongside one, are not fragile."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            git(repo, "init", "-q", "-b", "main")
            body = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(6))
            for name in ("a", "b", "c"):
                write(repo, f"src/{name}.py", body)
            write(repo, "version.py", "VERSION = '0'\n")
            write(repo, "README.md", "# Demo\n\nline\n")
            commit(repo, "Initial import")
            for n in range(1, 5):
                for name in ("a", "b", "c"):
                    write(repo, f"src/{name}.py", body.replace("return 1", f"return 1 + {n}") + f"# pass {n}\n")
                write(repo, "version.py", f"VERSION = '{n}'\n")
                write(repo, "README.md", f"# Demo\n\nline\n\n" + "".join(f"Note {i}.\n\nMore.\n\n" for i in range(n)))
                commit(repo, f"fix: wrong result in pass {n}", date=f"2021-02-0{n}T00:00:00+00:00")
            write(repo, "src/new.py", "def g():\n    return 0\n")
            commit(repo, "fix: add the missing helper", date="2021-03-01T00:00:00+00:00")
            self.assertEqual(run_cli("dig", "--repo", repo)[0], 0)
            files = model.build(cache.load(repo), "r", "2021-03-01")["files"]
            self.assertEqual(files["src/a.py"]["fixes"], 4)
            self.assertNotEqual(files["src/a.py"]["level"], "stable")
            self.assertEqual((files["version.py"]["fixes"], files["version.py"]["level"]), (0, "stable"))  # one-line bystander
            self.assertEqual((files["README.md"]["score"], files["README.md"]["prose"]), (0, True))
            self.assertEqual(files["src/new.py"]["fixes"], 0)  # created by a fix, not fixed by it
            partners = [c["path"] for c in files["src/a.py"]["coupled"] if c["strong"]]
            self.assertIn("src/b.py", partners)

    def test_committing_the_output_does_not_cause_churn(self):
        """A nightly job that commits the index must not make the next night's index differ."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            git(repo, "init", "-q", "-b", "main")
            write(repo, "a.py", "def f():\n    return 1\n")
            commit(repo, "Initial import")
            self.assertEqual(run_cli("dig", "--repo", repo)[0], 0)
            commit(repo, "chore(archaeology): refresh index", date="2021-01-02T00:00:00+00:00")
            self.assertIn("CODEBASE_ARCHAEOLOGY.md", git(repo, "show", "--name-only", "--format=", "HEAD"))
            code, _o, err = run_cli("dig", "--repo", repo)
            self.assertEqual(code, 0)
            self.assertIn("0 new commit(s) (1 total)", err)
            self.assertIn("nothing (outputs already up to date)", err)
            self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_rewritten_history_triggers_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            os.makedirs(repo)
            git(repo, "init", "-q", "-b", "main")
            write(repo, "a.py", "x = 1\n")
            commit(repo, "one")
            write(repo, "a.py", "x = 2\n")
            commit(repo, "two")
            self.assertEqual(run_cli("dig", "--repo", repo)[0], 0)
            git(repo, "reset", "-q", "--hard", "HEAD~1")
            write(repo, "a.py", "x = 3\n")
            commit(repo, "fix: three")
            code, _o, err = run_cli("dig", "--repo", repo)
            self.assertEqual(code, 0)
            self.assertIn("full run, 2 new commit(s)", err)
            self.assertEqual(len(cache.load(repo)["commits"]), 2)


if __name__ == "__main__":
    unittest.main()
