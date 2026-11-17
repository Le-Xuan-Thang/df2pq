"""'What's new' and 'Updates' dialog for the NiceGUI interface."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import subprocess
from typing import Any

from nicegui import app, run, ui

from .settings import Settings
from .updates import (
    RELEASES_URL,
    Release,
    UpdateError,
    apply_git_update,
    apply_package_update,
    can_prompt_for_admin,
    changelog_text,
    current_version,
    fetch_latest,
    install_kind,
    is_newer,
    manual_install_command,
    restart_command,
    system_env,
    update_command,
)

CHECK_INTERVAL = timedelta(hours=12)
KIND_LABELS = {
    "git": "Source checkout (git)",
    "package": "Python package install",
    "portable": "Portable build",
    "rpm": "Installed with the RPM package",
    "deb": "Installed with the DEB package",
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
                if kind == "git" or (kind in ("rpm", "deb") and can_prompt_for_admin()):
                    ui.button("Update now", icon="system_update_alt",
                              on_click=lambda: apply_git() if kind == "git"
                              else apply_package(release)).props(
                        "unelevated no-caps color=primary text-color=black")
            if kind in ("rpm", "deb"):
                if can_prompt_for_admin():
                    ui.label("Downloads the new package, checks it against the published "
                             "checksum and installs it; your system will ask for an "
                             "administrator password.").classes("df-muted text-caption")
                else:
                    copy_command(manual_install_command(kind, release),
                                 "Download the package from the release page, then run:")
            elif kind == "portable":
                ui.label("Download the new build from the release page and replace this "
                         "folder.").classes("df-muted text-caption")
            elif kind == "package":
                copy_command(update_command(kind))

    def copy_command(command: str, caption: str = "") -> None:
        if caption:
            ui.label(caption).classes("df-muted text-caption")
        with ui.row().classes("w-full items-center no-wrap gap-2"):
            ui.label(command).classes("df-log grow px-3 py-2")
            ui.button(icon="content_copy", on_click=lambda: (
                ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(command)})"),
                ui.notify("Command copied"))).props("flat round dense")

    def show_failure(error: Exception) -> None:
        result.clear()
        with result:
            ui.label("Update failed").classes("text-red-400 font-medium")
            text = str(error) if isinstance(error, UpdateError) else \
                f"{type(error).__name__}: {error}"
            ui.label(text).classes("df-log px-3 py-2 w-full").style("white-space:pre-wrap")

    def show_updated(lines: list[str]) -> None:
        result.clear()
        with result:
            ui.label("✓  Updated. Restart df2pq to use the new version.").classes(
                "text-green-400 font-medium")
            ui.label("\n".join(lines)).classes("df-log px-3 py-2 w-full").style(
                "white-space:pre-wrap")
            ui.button("Restart df2pq", icon="restart_alt", on_click=restart).props(
                "unelevated no-caps color=primary text-color=black")

    async def apply_package(release: Release) -> None:
        state = {"done": 0, "total": 0}
        result.clear()
        with result:
            status = ui.label("Downloading the update…").classes("df-muted")
            bar = ui.linear_progress(value=0, show_value=False, size="8px").props(
                "rounded color=primary track-color=grey-9")

        def tick() -> None:
            if state["total"]:
                bar.set_value(state["done"] / state["total"])
                status.set_text(f"Downloading the update… {state['done'] / 1e6:.0f} / "
                                f"{state['total'] / 1e6:.0f} MB")
            if state["total"] and state["done"] >= state["total"]:
                status.set_text("Installing — enter your password in the system dialog…")
                bar.props("indeterminate")

        with result:  # the clicked button was just removed; attach the timer here
            timer = ui.timer(0.3, tick)
        try:
            lines = await run.io_bound(
                apply_package_update, release, kind,
                lambda done, total: state.update(done=done, total=total))
        except Exception as exc:  # shown to the user, including unexpected errors
            timer.cancel()
            show_failure(exc)
            return
        timer.cancel()
        show_updated(lines)

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
        except Exception as exc:  # shown to the user, including unexpected errors
            show_failure(exc)
            return
        status.set_text("")
        show_updated(lines)

    def restart() -> None:
        # The packaged app is its own executable; "python -m df2pq" only works from source.
        subprocess.Popen(restart_command(), stdin=subprocess.DEVNULL, start_new_session=True,
                         env=system_env())
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
