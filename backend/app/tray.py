"""System-tray control for the crawler.

    python -m app.tray

Runs the continuous crawler in a background thread and puts an icon in the
notification area so it can be paused, resumed and stopped without killing
the process or finding its console window.

The icon colour is the status at a glance:
    green   crawling
    amber   paused
    blue    publishing a snapshot
    grey    idle (frontier exhausted, backing off)
    red     last cycle errored

Pausing is checked between individual API requests, so it takes effect
within about a second rather than at the end of a 200-match batch.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import webbrowser
from pathlib import Path

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - dependency is optional
    print(
        "The tray needs pystray and Pillow:\n    pip install pystray pillow",
        file=sys.stderr,
    )
    raise SystemExit(1)

from .analytics_db import AnalyticsDB
from .config import load_env
from .crawler import CrawlControl, Crawler, run_forever

STATUS_COLORS = {
    "running": (46, 194, 126),
    "paused": (245, 165, 36),
    "publishing": (76, 141, 255),
    "idle": (120, 130, 150),
    "retrying": (255, 70, 85),
    "stopping": (120, 130, 150),
    "stopped": (90, 95, 110),
    "starting": (120, 130, 150),
}


def make_icon(status: str) -> "Image.Image":
    """A filled circle in the status colour, with a pause bar when paused.

    Drawn rather than shipped as a file so the icon can change with state
    and there is no asset to keep in sync.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colour = STATUS_COLORS.get(status, STATUS_COLORS["idle"])

    draw.ellipse([2, 2, size - 3, size - 3], fill=(14, 18, 28, 255))
    draw.ellipse([6, 6, size - 7, size - 7], fill=colour + (255,))

    if status == "paused":
        # Two bars, the universal pause glyph.
        draw.rectangle([23, 20, 29, 44], fill=(14, 18, 28, 255))
        draw.rectangle([35, 20, 41, 44], fill=(14, 18, 28, 255))
    elif status in {"stopped", "stopping"}:
        draw.rectangle([22, 22, 42, 42], fill=(14, 18, 28, 255))
    else:
        # A play triangle for every active state.
        draw.polygon([(26, 19), (26, 45), (46, 32)], fill=(14, 18, 28, 255))
    return img


class TrayApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.control = CrawlControl()
        self.icon: pystray.Icon | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._last_status = ""

    # --- menu -----------------------------------------------------------
    def _status_text(self, _item: object = None) -> str:
        c = self.control
        if c.status == "paused":
            return "Paused"
        if c.status == "idle":
            return "Idle — no new matches"
        if c.status == "publishing":
            return "Publishing snapshot…"
        if c.status == "retrying":
            return "Retrying after an error"
        if c.status in {"stopping", "stopped"}:
            return "Stopped"
        return f"Crawling — cycle {c.cycle}"

    def _dataset_text(self, _item: object = None) -> str:
        c = self.control
        if not c.db_matches:
            return "Dataset: reading…"
        return f"Dataset: {c.db_matches:,} matches · {c.db_kills:,} kills"

    def _session_text(self, _item: object = None) -> str:
        return f"This run: +{self.control.stored_this_run:,} · published {self.control.last_publish}"

    def _error_text(self, _item: object = None) -> str:
        return f"Last error: {self.control.last_error}" if self.control.last_error else ""

    def _toggle(self, _icon: object = None, _item: object = None) -> None:
        self.control.toggle()
        self.refresh()

    def _publish_now(self, _icon: object = None, _item: object = None) -> None:
        def run() -> None:
            from datetime import datetime, timezone

            from .publish import publish

            previous = self.control.status
            self.control.status = "publishing"
            self.refresh()
            try:
                AnalyticsDB().set_meta(
                    "generated_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
                )
                publish(verbose=False)
                self.control.last_publish = datetime.now().strftime("%H:%M")
                self.control.last_error = ""
            except Exception as exc:
                self.control.last_error = f"publish: {str(exc)[:100]}"
            finally:
                self.control.status = "paused" if self.control.paused else previous
                self.refresh()

        threading.Thread(target=run, daemon=True).start()

    def _open_data_dir(self, _icon: object = None, _item: object = None) -> None:
        path = Path(AnalyticsDB().path).parent
        os.startfile(path)  # noqa: S606 - Windows shell open, path is ours

    def _open_site(self, _icon: object = None, _item: object = None) -> None:
        webbrowser.open(os.environ.get("VALHEATMAP_SITE_URL", "http://127.0.0.1:8000"))

    def _quit(self, icon: pystray.Icon, _item: object = None) -> None:
        self.control.stop()
        icon.visible = False
        icon.stop()

    def build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.MenuItem(self._dataset_text, None, enabled=False),
            pystray.MenuItem(self._session_text, None, enabled=False),
            pystray.MenuItem(
                self._error_text, None, enabled=False,
                visible=lambda _i: bool(self.control.last_error),
            ),
            pystray.Menu.SEPARATOR,
            # default=True makes this the left-click action, so a single
            # click on the icon pauses or resumes.
            pystray.MenuItem(
                lambda _i: "Resume crawling" if self.control.paused else "Pause crawling",
                self._toggle,
                default=True,
            ),
            pystray.MenuItem("Publish snapshot now", self._publish_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open data folder", self._open_data_dir),
            pystray.MenuItem("Open site", self._open_site),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def refresh(self) -> None:
        if self.icon is None:
            return
        if self.control.status != self._last_status:
            self.icon.icon = make_icon(self.control.status)
            self._last_status = self.control.status
        self.icon.title = f"ValHeatMap — {self._status_text()}"
        self.icon.update_menu()

    # --- crawler thread --------------------------------------------------
    def _crawl(self) -> None:
        load_env()
        key = os.environ.get("HENRIK_API_KEY")
        if not key:
            self.control.status = "retrying"
            self.control.last_error = "HENRIK_API_KEY is not set"
            self.refresh()
            return

        crawler = Crawler(
            api_key=key,
            region=self.args.region,
            rate_limit=int(os.environ.get("HENRIK_RATE_LIMIT", self.args.rate)),
            verbose=self.args.verbose,
            control=self.control,
        )
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(
                run_forever(
                    crawler,
                    batch=self.args.batch,
                    publish_every=self.args.publish_every,
                    control=self.control,
                )
            )
        except Exception as exc:  # keep the tray alive if the loop dies
            self.control.status = "retrying"
            self.control.last_error = str(exc)[:120]
        finally:
            self._loop.close()

    def _poll(self) -> None:
        """Refresh the menu periodically so counters stay current."""
        while not self.control.stopping:
            self.refresh()
            threading.Event().wait(3)

    def run(self) -> int:
        if self.args.start_paused:
            self.control.pause()

        self._thread = threading.Thread(target=self._crawl, daemon=True)
        self._thread.start()
        threading.Thread(target=self._poll, daemon=True).start()

        self.icon = pystray.Icon(
            "valheatmap",
            make_icon(self.control.status),
            "ValHeatMap — starting",
            self.build_menu(),
        )
        # Blocks until Quit; the crawler thread is a daemon so it exits with us.
        self.icon.run()
        self.control.stop()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the crawler with a tray icon.")
    parser.add_argument("--region", default=os.environ.get("RIOT_REGION", "na"))
    parser.add_argument("--batch", type=int, default=200, help="matches per cycle")
    parser.add_argument(
        "--publish-every", type=int, default=2000,
        help="publish a snapshot after this many new matches (0 disables)",
    )
    parser.add_argument("--rate", type=int, default=90, help="requests/min budget")
    parser.add_argument("--start-paused", action="store_true")
    parser.add_argument(
        "--verbose", action="store_true", help="also log to the console"
    )
    return TrayApp(parser.parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
