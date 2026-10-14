"""'What's new' and 'Updates' dialog for the NiceGUI interface."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import subprocess
import sys
from typing import Any

from nicegui import app, run, ui

from .settings import Settings
from .updates import (
    RELEASES_URL,
    Release,
    UpdateError,
    apply_git_update,
    changelog_text,
    current_version,
    fetch_latest,
    install_kind,
    is_newer,
    update_command,
)

CHECK_INTERVAL = timedelta(hours=12)
KIND_LABELS = {
    "git": "Source checkout (git)",
    "package": "Python package install",
    "portable": "Portable build",
}


def cached_update(settings: Settings) -> str | None:
    """Tag of a newer release found by an earlier check, if any."""
    tag = settings.get("latest_tag")
    return tag if tag and is_newer(tag, current_version()) else None


def check_is_due(settings: Settings) -> bool:
    if not settings.get("update_check", True):
        return False
    try:
        last = datetime.fromisoformat(settings.get("last_check", ""))
    except ValueError:
        return True
    return datetime.now() - last > CHECK_INTERVAL


async def check_for_updates(settings: Settings) -> tuple[Release | None, str | None]:
    """Query GitHub off the event loop and cache the outcome. Returns (release, error)."""
    try:
        release = await run.io_bound(fetch_latest)
    except UpdateError as exc:
        return None, str(exc)
    settings.set("last_check", datetime.now().isoformat(timespec="seconds"))
    settings.set("latest_tag", release.tag if release else "")
    settings.save()
    return release, None


async def show_about(settings: Settings, tab: str = "changelog", on_checked: Any = None) -> None:
    version, kind = current_version(), install_kind()
    release_box: dict[str, Any] = {}

    with ui.dialog().props("no-shake") as dialog, ui.card().classes("df-card p-0 no-wrap").style(
            "width:min(860px,95vw);max-width:none;height:min(700px,92vh)"):
        with ui.row().classes("w-full items-center justify-between no-wrap px-6 pt-5 pb-2"):
            with ui.column().classes("gap-0"):
                ui.label(f"df2pq {version}").classes("df-card-title")
                ui.label(KIND_LABELS[kind]).classes("df-muted text-caption")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense")
        with ui.tabs(value=tab).props("dense align=left").classes("w-full px-4") as tabs:
            ui.tab("changelog", label="What's new")
            ui.tab("updates", label="Updates")
        with ui.tab_panels(tabs, value=tab).classes("w-full grow bg-transparent").style(
                "min-height:0;overflow:auto"):
            with ui.tab_panel("changelog").classes("px-6"):
                ui.markdown(changelog_text()).classes("df-markdown")
            with ui.tab_panel("updates").classes("px-6 gap-3"):
                with ui.row().classes("w-full items-center gap-3 no-wrap"):
                    check_btn = ui.button("Check for updates", icon="refresh").props(
                        "unelevated no-caps color=primary text-color=black")
                    last = ui.label().classes("df-muted text-caption")
                ui.switch("Check automatically on startup", value=settings.get("update_check", True),
                          on_change=lambda e: (settings.set("update_check", e.value),
                                               settings.save()))
                result = ui.column().classes("w-full gap-3")

    def stamp() -> None:
        when = settings.get("last_check")
        last.set_text(f"Last checked {when.replace('T', ' ')}" if when else "Not checked yet")

    def render(release: Release | None, error: str | None) -> None:
        result.clear()
        with result:
            if error:
                ui.label(error).classes("text-red-400")
                return
            if release is None:
                ui.label(f"No releases are published yet — you are running df2pq {version}."
                         ).classes("df-muted")
                return
            if not is_newer(release.tag, version):
                ui.label(f"✓  You are up to date ({version}).").classes("text-green-400 font-medium")
                return
            ui.label(f"Update available: {release.version}"
                     + (f"  ·  published {release.published}" if release.published else "")
                     ).classes("text-amber-400 text-base font-medium")
            ui.markdown(release.notes or "_This release has no notes._").classes(
                "df-markdown w-full")
            with ui.row().classes("items-center gap-2"):
                ui.button("Open release page", icon="open_in_new",
                          on_click=lambda: ui.navigate.to(release.url, new_tab=True)
                          ).props("outline no-caps")
                if kind == "git":
                    ui.button("Update now", icon="system_update_alt",
                              on_click=lambda: apply_git()).props(
                        "unelevated no-caps color=primary text-color=black")
            if kind == "portable":
                ui.label("Download the new build from the release page and replace this "
                         "folder.").classes("df-muted text-caption")
            elif kind == "package":
                command = update_command(kind)
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.label(command).classes("df-log grow px-3 py-2")
                    ui.button(icon="content_copy", on_click=lambda: (
                        ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(command)})"),
                        ui.notify("Command copied"))).props("flat round dense")

    async def do_check() -> None:
        check_btn.set_enabled(False)
        check_btn.props("loading")
        release, error = await check_for_updates(settings)
        check_btn.props(remove="loading")
        check_btn.set_enabled(True)
        release_box["release"] = release
        stamp()
        render(release, error)
        if on_checked:
            on_checked()

    async def apply_git() -> None:
        result.clear()
        with result:
            status = ui.label("Updating…").classes("df-muted")
            ui.spinner(size="sm")
        try:
            lines = await run.io_bound(apply_git_update)
        except UpdateError as exc:
            result.clear()
            with result:
                ui.label("Update failed").classes("text-red-400 font-medium")
                ui.label(str(exc)).classes("df-log px-3 py-2 w-full").style("white-space:pre-wrap")
            return
        except Exception as exc:  # unexpected: show it instead of silently failing
            result.clear()
            with result:
                ui.label(f"Update failed: {type(exc).__name__}: {exc}").classes("text-red-400")
            return
        status.set_text("")
        result.clear()
        with result:
            ui.label("✓  Updated. Restart df2pq to use the new version.").classes(
                "text-green-400 font-medium")
            ui.label("\n".join(lines)).classes("df-log px-3 py-2 w-full").style(
                "white-space:pre-wrap")
            ui.button("Restart df2pq", icon="restart_alt", on_click=restart).props(
                "unelevated no-caps color=primary text-color=black")

    def restart() -> None:
        subprocess.Popen([sys.executable, "-m", "df2pq"], stdin=subprocess.DEVNULL)
        app.shutdown()

    check_btn.on_click(do_check)
    stamp()
    with result:
        ui.label(f"Current version: {version}. Press “Check for updates” to look for a "
                 f"newer release.").classes("df-muted")
        ui.link("Browse all releases on GitHub", RELEASES_URL, new_tab=True)
    dialog.open()
    await dialog
    dialog.delete()
