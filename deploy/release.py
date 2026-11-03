#!/usr/bin/env python3
"""release.py: publish main to the public GitHub repo as ONE new commit, and pack the install branch as a bundle.

Run on the dev machine, in the naypyers checkout (Forgejo = origin). Nothing private leaves:
  - GitHub gets a commit with main's current files on top of GitHub's own main (its parent is the previous
    public commit, not the private history), so the public history stays linear and servers can `git pull`.
  - the install branch (Hostinger-only files) never goes to GitHub: it becomes a bundle file you copy to the server.

  release.py check                      on main, clean, same as origin/main; nothing forbidden in the files
  release.py github [--push]            show the release commit (dry run); --push sends it to GitHub (no force)
  release.py install-bundle [--out F]   bundle of origin/<install branch> as branch <install branch>
                                        (default ./naypyers-install.bundle)

Options: --github-url URL (default the public repo, SSH), --install-branch NAME (default hostinger).
Private names that must never be published: one per line in ~/.config/naypyers/release-deny.txt (not in git).
Errors print "step <name> failed: <reason>" and exit 1."""

##### CONFIG #####
import argparse, re, subprocess, sys, tempfile
from datetime import date
from pathlib import Path

GITHUB_URL = "git@github.com:lucian-pdev/n8n_python_auto-scaling_health-checks.git"
DENY_FILE = Path.home() / ".config" / "naypyers" / "release-deny.txt"
FORBIDDEN_PATHS = re.compile(r"(^|/)(PUBLISH_REMINDER\.md|not-commited/|\.env$|\.env\.|db/.+|.*\.bundle$)")
SECRET_PATTERNS = re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|"
                             r"-----BEGIN [A-Z ]*PRIVATE KEY-----(\\n|$)|AKIA[0-9A-Z]{16}")   # a real key: header ends the line, or \n follows (JSON)


##### ERRORS #####
class StepFailed(Exception):
    """A step could not finish; the message says why."""


##### HELPERS #####
def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise StepFailed(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def fetch_github(url):
    """GitHub's main into refs/release/github-main (no remote config changes)."""
    git("fetch", "-q", url, "+refs/heads/main:refs/release/github-main")
    return git("rev-parse", "refs/release/github-main")


##### STEPS #####
def step_check(o):
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise StepFailed("not on main: git checkout main")
    git("fetch", "-q", "origin")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise StepFailed("tracked files have local changes: commit or stash them first")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        raise StepFailed("main differs from origin/main: git pull --ff-only (and push what's yours)")
    files = git("ls-tree", "-r", "--name-only", "origin/main").splitlines()
    bad = [f for f in files if FORBIDDEN_PATHS.search(f)]
    if bad:
        raise StepFailed(f"files that must never be published are tracked: {bad}")
    deny = [w.strip() for w in DENY_FILE.read_text().splitlines()
            if w.strip() and not w.startswith("#")] if DENY_FILE.exists() else []
    if not deny:
        print(f"  WARN no deny list at {DENY_FILE}: private names are not checked")
    hits = []
    for pattern, what in [(SECRET_PATTERNS.pattern, "secret-like")] + [(re.escape(w), "deny-listed") for w in deny]:
        out = git("grep", "-IlE" if what == "secret-like" else "-IliE", pattern, "origin/main", "--", ".", check=False)
        hits += [f"{what}: {l.split(':', 1)[1]}" for l in out.splitlines()]
    if hits:
        raise StepFailed(f"found in the files to publish: {hits}")
    print(f"  main {git('rev-parse', '--short', 'HEAD')}: {len(files)} files, nothing forbidden "
          f"({len(deny)} deny-listed names checked)")


def step_github(o):
    step_check(o)
    parent = fetch_github(o.github_url)
    tree = git("rev-parse", "origin/main^{tree}")
    if git("rev-parse", f"{parent}^{{tree}}") == tree:
        print("  GitHub's main already has exactly these files: nothing to release")
        return
    msg = f"naypyers {date.today():%Y-%m-%d} (main {git('rev-parse', '--short', 'origin/main')})"
    commit = git("commit-tree", tree, "-p", parent, "-m", msg)
    print(f"  release commit {commit[:10]}: '{msg}', parent = GitHub main {parent[:10]}")
    print("  " + "\n  ".join(git("diff", "--stat", parent, commit).splitlines()[-1:]))
    if not o.push:
        print("  dry run: rerun with --push to publish it")
        return
    git("push", o.github_url, f"{commit}:refs/heads/main")       # fast-forward only: no --force
    print(f"  pushed to {o.github_url} main")


def step_install_bundle(o):
    git("fetch", "-q", "origin")
    ref = f"origin/{o.install_branch}"
    out = Path(o.out).resolve()
    with tempfile.TemporaryDirectory() as tmp:        # throwaway repo: only that branch, named as on Forgejo
        git("init", "-q", "--bare", tmp)
        git("--git-dir", tmp, "fetch", "-q", str(Path.cwd()), f"+refs/remotes/{ref}:refs/heads/{o.install_branch}")
        git("--git-dir", tmp, "bundle", "create", "-q", str(out), o.install_branch)
    print(f"  {out}: branch {o.install_branch} at {git('rev-parse', '--short', ref)} "
          f"({git('log', '-1', '--format=%s', ref)})")


STEPS = {"check": step_check, "github": step_github, "install-bundle": step_install_bundle}


##### RUNNER #####
def main(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("cmd", nargs="?")
    p.add_argument("--push", action="store_true")
    p.add_argument("--out", default="naypyers-install.bundle")
    p.add_argument("--github-url", default=GITHUB_URL)
    p.add_argument("--install-branch", default="hostinger")
    o = p.parse_args(argv)
    if o.cmd not in STEPS:
        sys.exit(__doc__)
    try:
        STEPS[o.cmd](o)
    except StepFailed as e:
        print(f"step {o.cmd} failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
