"""The installed `parallax` command.

`python -m parallax` remains exactly what it was and takes the same flags. This
adds the part that makes it a tool rather than a script: an entry point on the
PATH, a project file so the settings that never change are written down once,
and a `doctor` that answers "will this work here" before a sweep spends four
minutes discovering that it will not.

Precedence is the ordinary one and is worth stating because getting it backwards
is a silent-wrong-answer bug: a flag beats the file, the file beats the default.
The reason to type a flag is to override what is written down.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import config
from .__main__ import main as sweep_main


def _sweep(args: argparse.Namespace, extra: list[str]) -> int:
    settings = config.load(Path(args.config) if args.config else None)
    argv: list[str] = []

    url = getattr(args, "url", None) or settings.url
    if not url:
        raise SystemExit(
            "no target: pass a URL, or set target.url in parallax.toml "
            "(run `parallax init` to create one)"
        )
    argv.append(url)

    # Only settings the caller did not already give on the command line are
    # taken from the file, so `parallax sweep --out somewhere` means it.
    def unless_given(flag: str, value: object) -> None:
        if value in (None, "", False) or flag in extra:
            return
        argv.extend([flag, str(value)])

    unless_given("--out", settings.out)
    unless_given("--max-surfaces", settings.max_surfaces)
    unless_given("--credentials", settings.credentials)
    unless_given("--relational-scenarios", settings.scenarios)
    if "--deny" not in extra:
        for pattern in settings.deny:
            argv.extend(["--deny", pattern])
    if "--locale" not in extra and settings.locales is not None:
        for tag in settings.locales:
            argv.extend(["--locale", tag])
    if "--viewport" not in extra and settings.viewports is not None:
        for size in settings.viewports:
            argv.extend(["--viewport", size])
    unless_given("--open-pr", settings.open_pr)
    unless_given("--pr-base", settings.pr_base)
    if settings.propose_scenarios and "--propose-scenarios" not in extra:
        argv.append("--propose-scenarios")
    if settings.vision is False and "--no-vision" not in extra:
        argv.append("--no-vision")

    # Environment the file may set, but never override something already there:
    # an exported variable is a deliberate act by whoever ran the command.
    for name, value in (
        ("GOOGLE_CLOUD_PROJECT", settings.google_cloud_project),
        ("PARALLAX_GEMMA_URL", settings.gemma_url),
    ):
        if value and not os.environ.get(name):
            os.environ[name] = value

    if settings.source and not args.quiet:
        print(f"parallax: {settings.describe()}", file=sys.stderr)
    return sweep_main([*argv, *extra])


def _init(args: argparse.Namespace, _extra: list[str]) -> int:
    target = Path(args.directory or ".") / config.CONFIG_NAME
    if target.exists() and not args.force:
        print(f"{target} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    target.write_text(config.TEMPLATE, encoding="utf-8")
    print(f"wrote {target}")
    print("Edit target.url, then run: parallax sweep")
    return 0


def _doctor(_args: argparse.Namespace, _extra: list[str]) -> int:
    """Answer "will a sweep work here" before one spends four minutes finding out."""
    settings = config.load()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("configuration", settings.source is not None, settings.describe()))
    checks.append(("target url", bool(settings.url), settings.url or "not set — pass a URL or set target.url"))

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            version = browser.version
            browser.close()
        checks.append(("chromium", True, version))
    except Exception as error:
        checks.append((
            "chromium", False,
            f"{type(error).__name__}: run `python -m playwright install chromium`",
        ))

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or settings.google_cloud_project
    if project:
        token = shutil.which("gcloud") is not None
        adc = (Path.home() / ".config/gcloud/application_default_credentials.json").is_file()
        checks.append((
            "vertex ai", token or adc,
            f"project {project}" + ("" if token or adc else " — no ADC and no gcloud on PATH"),
        ))
    else:
        checks.append(("vertex ai", False, "no GOOGLE_CLOUD_PROJECT; the model lenses will be disabled"))

    if settings.credentials:
        exists = settings.credentials.is_file()
        mode = oct(settings.credentials.stat().st_mode & 0o777)[2:] if exists else "-"
        # A world-readable secret is worth a warning even when it works.
        checks.append((
            "credentials", exists,
            f"{settings.credentials} (mode {mode})" if exists else f"{settings.credentials} is missing",
        ))
        if exists and mode not in {"600", "400"}:
            checks.append(("credentials mode", False, f"mode {mode} is readable by others; chmod 600 it"))

    if settings.open_pr:
        has_token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
        checks.append(("pull requests", has_token, settings.open_pr if has_token else "no GITHUB_TOKEN"))

    gemma = os.environ.get("PARALLAX_GEMMA_URL") or settings.gemma_url
    checks.append(("finding triage", bool(gemma), gemma or "no PARALLAX_GEMMA_URL; grouping is disabled"))

    width = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'note'}  {name.ljust(width)}  {detail}")

    # Only a broken browser or an unusable target stops a sweep; everything else
    # degrades and says so, which is the whole design.
    fatal = [name for name, ok, _ in checks if not ok and name in {"chromium", "target url"}]
    if fatal:
        print(f"\ncannot sweep: {', '.join(fatal)}", file=sys.stderr)
        return 1
    print("\nready to sweep")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The assistant has its own parser; the existing sweep CLI stays compatible.
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "assistant":
        from .assistant.web import main as assistant_main
        return assistant_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="parallax",
        description="Relational browser regression: point it at a URL, get failing tests back.",
    )
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("assistant", help="launch the local personal browser assistant")

    sweep = subcommands.add_parser("sweep", help="witness an application and emit failing specs")
    # The URL is deliberately not an argparse positional. Every other flag is
    # passed through to the sweep parser unparsed, and argparse will happily
    # bind the *value* of an unknown flag to a positional — so
    # `parallax sweep --out runs/x` read "runs/x" as the target. It is taken
    # from the leading token instead, where it unambiguously is one.
    sweep.add_argument("--config", help=f"path to {config.CONFIG_NAME}")
    sweep.add_argument("--quiet", action="store_true", help="do not announce the configuration file")
    sweep.set_defaults(handler=_sweep)

    created = subcommands.add_parser("init", help=f"write a {config.CONFIG_NAME} to start from")
    created.add_argument("directory", nargs="?", help="where to write it; defaults to here")
    created.add_argument("--force", action="store_true", help="overwrite an existing file")
    created.set_defaults(handler=_init)

    check = subcommands.add_parser("doctor", help="check that a sweep can run here, before running one")
    check.set_defaults(handler=_doctor)

    argv = list(sys.argv[1:] if argv is None else argv)
    leading: str | None = None
    if len(argv) > 1 and argv[0] == "sweep" and not argv[1].startswith("-"):
        leading = argv.pop(1)

    known, extra = parser.parse_known_args(argv)
    if not getattr(known, "handler", None):
        parser.print_help()
        return 1
    known.url = leading
    return known.handler(known, extra)


if __name__ == "__main__":
    raise SystemExit(main())
