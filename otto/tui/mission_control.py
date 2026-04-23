"""Textual Mission Control app."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Log, Static

from otto.tui.mission_control_model import (
    MissionControlFilters,
    MissionControlModel,
    MissionControlState,
    PaneName,
)


class HelpModal(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="help-modal"):
            yield Static(
                "\n".join(
                    [
                        "Mission Control",
                        "",
                        "Tab / Shift-Tab: cycle panes",
                        "1 / 2 / 3: focus Live / History / Detail",
                        "j/k or Up/Down: move selection",
                        "Enter: pin selection and focus Detail",
                        "Esc: return to originating list",
                        "a: toggle active-only live rows",
                        "t: cycle type filter",
                        "f: cycle history outcome filter",
                        "/: substring history filter",
                        "[ / ]: history page",
                        "o: cycle logs",
                        "s: toggle log follow",
                        "Home / End: top / resume follow",
                        "?: help",
                        "",
                        "Action keys are placeholders in Phase 2; Detail shows what each would do.",
                    ]
                ),
                id="help-body",
            )

    def action_close(self) -> None:
        self.dismiss(None)


class FilterModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "apply", "Apply"),
    ]

    def __init__(self, initial_value: str) -> None:
        super().__init__()
        self._initial_value = initial_value

    def compose(self) -> ComposeResult:
        with Container(id="filter-modal"):
            yield Static("History Filter", id="filter-title")
            yield Input(
                value=self._initial_value,
                placeholder="intent, branch, task id, run id",
                id="filter-input",
            )
            yield Static("Enter apply / Esc cancel", id="filter-help")

    def on_mount(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        self.dismiss(self.query_one("#filter-input", Input).value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class MissionControlApp(App[int]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #banner, #status-bar, #footer {
        padding: 0 1;
    }

    #banner {
        color: yellow;
        height: 1;
    }

    #main {
        height: 1fr;
    }

    .pane {
        border: round $panel;
        padding: 0 1;
        height: 1fr;
    }

    .pane.focused {
        border: round $accent;
    }

    .pane-title {
        color: $accent;
        padding-bottom: 1;
    }

    #live-pane {
        width: 32%;
    }

    #history-pane {
        width: 28%;
    }

    #detail-pane {
        width: 40%;
    }

    #detail-meta, #detail-actions {
        height: auto;
    }

    #detail-artifacts {
        height: 8;
    }

    #detail-log {
        height: 1fr;
    }

    #status-bar {
        color: green;
        height: auto;
    }

    #footer {
        color: cyan;
        height: auto;
    }

    #help-modal {
        width: 72;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }

    #filter-modal {
        width: 60;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }

    #filter-title {
        color: $accent;
        padding-bottom: 1;
    }

    #filter-help {
        color: $text-muted;
        padding-top: 1;
    }
    """

    BINDINGS = [
        Binding("tab", "cycle_focus_forward", "Next Pane", show=False, priority=True),
        Binding("shift+tab", "cycle_focus_backward", "Prev Pane", show=False, priority=True),
        Binding("1", "focus_live", "Live", show=False, priority=True),
        Binding("2", "focus_history", "History", show=False, priority=True),
        Binding("3", "focus_detail", "Detail", show=False, priority=True),
        Binding("left", "focus_left", "Left", show=False),
        Binding("right", "focus_right", "Right", show=False),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("k", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("j", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "open_detail", "Detail", show=False, priority=True),
        Binding("escape", "return_to_origin", "Back", show=False, priority=True),
        Binding("a", "toggle_active_only", "Active Only", show=False),
        Binding("t", "cycle_type_filter", "Type Filter", show=False),
        Binding("f", "cycle_outcome_filter", "Outcome Filter", show=False),
        Binding("/", "open_query_filter", "Query Filter", show=False),
        Binding("[", "history_prev_page", "Prev Page", show=False),
        Binding("]", "history_next_page", "Next Page", show=False),
        Binding("pageup", "history_prev_page", "Prev Page", show=False),
        Binding("pagedown", "history_next_page", "Next Page", show=False),
        Binding("o", "cycle_logs", "Cycle Logs", show=False),
        Binding("s", "toggle_follow", "Toggle Follow", show=False),
        Binding("home", "log_top", "Log Top", show=False),
        Binding("end", "log_bottom", "Log Bottom", show=False),
        Binding("c", "preview_action('c')", "Cancel", show=False),
        Binding("r", "preview_action('r')", "Resume", show=False),
        Binding("R", "preview_action('R')", "Retry", show=False),
        Binding("x", "preview_action('x')", "Cleanup", show=False),
        Binding("m", "preview_action('m')", "Merge", show=False),
        Binding("e", "preview_action('e')", "Edit", show=False),
        Binding("?", "show_help", "Help", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        project_dir: Path,
        *,
        initial_filters: MissionControlFilters | None = None,
        dashboard_mouse: bool = False,
        queue_compat: bool = False,
    ) -> None:
        super().__init__()
        self.project_dir = Path(project_dir)
        self.dashboard_mouse = dashboard_mouse
        self.queue_compat = queue_compat
        self.model = MissionControlModel(self.project_dir, queue_compat=queue_compat)
        self.state: MissionControlState = self.model.initial_state(
            filters=initial_filters or MissionControlFilters(),
            focus="live",
        )
        self._refresh_timer = None
        self._log_tailer = self.model.log_tailer(self.state)
        self._follow_log = True
        self._live_row_ids: list[str] = []
        self._history_row_ids: list[str] = []
        self._artifact_paths: list[str] = []
        self._filter_return_pane: PaneName = "history"

    def compose(self) -> ComposeResult:
        yield Static("", id="banner")
        with Horizontal(id="main"):
            with Vertical(id="live-pane", classes="pane"):
                yield Static("1. Live Runs", classes="pane-title")
                yield DataTable(id="live-table")
            with Vertical(id="history-pane", classes="pane"):
                yield Static("2. History", classes="pane-title")
                yield DataTable(id="history-table")
            with Vertical(id="detail-pane", classes="pane"):
                yield Static("3. Detail + Logs", classes="pane-title")
                yield Static("", id="detail-meta")
                yield DataTable(id="detail-artifacts")
                yield Static("", id="detail-actions")
                yield Log(id="detail-log")
        yield Static("", id="status-bar")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self._configure_tables()
        self._render_state()
        self._schedule_refresh()
        self._focus_pane(self.state.focus)

    def action_quit(self) -> None:
        self.exit(0)

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def action_focus_live(self) -> None:
        self._focus_pane("live")

    def action_focus_history(self) -> None:
        self._focus_pane("history")

    def action_focus_detail(self) -> None:
        self._focus_pane("detail")

    def action_focus_left(self) -> None:
        self._focus_pane(self._cycle_pane(-1))

    def action_focus_right(self) -> None:
        self._focus_pane(self._cycle_pane(1))

    def action_cycle_focus_forward(self) -> None:
        self._focus_pane(self._cycle_pane(1))

    def action_cycle_focus_backward(self) -> None:
        self._focus_pane(self._cycle_pane(-1))

    def action_cursor_down(self) -> None:
        if self.state.focus == "live":
            self._move_live_cursor(1)
        elif self.state.focus == "history":
            self._move_history_cursor(1)
        else:
            self._move_artifact_cursor(1)

    def action_cursor_up(self) -> None:
        if self.state.focus == "live":
            self._move_live_cursor(-1)
        elif self.state.focus == "history":
            self._move_history_cursor(-1)
        else:
            self._move_artifact_cursor(-1)

    def action_open_detail(self) -> None:
        self._focus_pane("detail")

    def action_return_to_origin(self) -> None:
        self._focus_pane(self.state.selection.origin_pane)

    def action_toggle_active_only(self) -> None:
        self.state.filters.active_only = not self.state.filters.active_only
        self._refresh_state()

    def action_cycle_type_filter(self) -> None:
        self.model.cycle_type_filter(self.state)
        self._refresh_state()

    def action_cycle_outcome_filter(self) -> None:
        self.model.cycle_outcome_filter(self.state)
        self._refresh_state()

    def action_open_query_filter(self) -> None:
        self._filter_return_pane = self.state.focus if self.state.focus in {"live", "history"} else self.state.selection.origin_pane
        self.push_screen(FilterModal(self.state.filters.query), self._apply_query_filter)

    def action_history_prev_page(self) -> None:
        self.model.previous_history_page(self.state)
        self._refresh_state()

    def action_history_next_page(self) -> None:
        self.model.next_history_page(self.state)
        self._refresh_state()

    def action_cycle_logs(self) -> None:
        detail = self.model.detail_view(self.state)
        if detail is None or not detail.log_paths:
            self.notify("no logs to cycle", severity="warning")
            return
        self.state.selection.log_index = (detail.selected_log_index + 1) % len(detail.log_paths)
        self._rebuild_log_tailer()
        self._render_detail()
        self._poll_log(clear_only=False)

    def action_toggle_follow(self) -> None:
        self._follow_log = not self._follow_log
        self.query_one("#detail-log", Log).auto_scroll = self._follow_log
        self._render_footer()

    def action_log_top(self) -> None:
        self._follow_log = False
        log_widget = self.query_one("#detail-log", Log)
        log_widget.auto_scroll = False
        log_widget.scroll_home(animate=False)
        self._render_footer()

    def action_log_bottom(self) -> None:
        self._follow_log = True
        log_widget = self.query_one("#detail-log", Log)
        log_widget.auto_scroll = True
        log_widget.scroll_end(animate=False)
        self._render_footer()

    def action_preview_action(self, key: str) -> None:
        detail = self.model.detail_view(self.state)
        if detail is None:
            self.notify("no selection", severity="warning")
            return
        for action in detail.legal_actions:
            if action.key == key:
                message = action.preview if action.enabled else (action.reason or action.preview)
                self.notify(message, severity="information" if action.enabled else "warning")
                return
        self.notify("action unavailable", severity="warning")

    def _tick(self) -> None:
        self._refresh_state()

    def _refresh_state(self) -> None:
        prior_log_path = self.model.detail_view(self.state).selected_log_path if self.model.detail_view(self.state) else None
        self.state = self.model.refresh(self.state)
        self._render_state()
        current_log_path = self.model.detail_view(self.state).selected_log_path if self.model.detail_view(self.state) else None
        if current_log_path != prior_log_path:
            self._rebuild_log_tailer()
        self._poll_log(clear_only=False)
        self._schedule_refresh()

    def _render_state(self) -> None:
        self._render_banner()
        self._render_live()
        self._render_history()
        self._render_detail()
        self._render_status()
        self._render_footer()
        self._highlight_panes()

    def _render_banner(self) -> None:
        self.query_one("#banner", Static).update(self.state.last_event_banner or "")

    def _render_live(self) -> None:
        table = self.query_one("#live-table", DataTable)
        table.clear(columns=False)
        self._live_row_ids = []
        for item in self.state.live_runs.items:
            status = item.overlay.label if item.overlay is not None else item.record.status.upper()
            table.add_row(
                status,
                item.record.run_type,
                item.display_id,
                item.branch_task,
                item.elapsed_display,
                item.cost_display,
                item.event,
            )
            self._live_row_ids.append(item.record.run_id)
        self._sync_live_cursor()

    def _render_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear(columns=False)
        self._history_row_ids = []
        for item in self.state.history_page.items:
            row = item.row
            display_id = row.queue_task_id or row.run_id
            table.add_row(
                item.completed_at_display,
                item.outcome_display,
                row.run_type,
                display_id,
                item.duration_display,
                item.cost_display,
                item.summary,
            )
            self._history_row_ids.append(row.run_id)
        self._sync_history_cursor()

    def _render_detail(self) -> None:
        detail = self.model.detail_view(self.state)
        meta = self.query_one("#detail-meta", Static)
        artifacts = self.query_one("#detail-artifacts", DataTable)
        actions = self.query_one("#detail-actions", Static)
        artifacts.clear(columns=False)
        self._artifact_paths = []

        if detail is None:
            meta.update("No selection.")
            actions.update("")
            self.query_one("#detail-log", Log).clear()
            return

        started = detail.record.timing.get("started_at") or "-"
        finished = detail.record.timing.get("finished_at") or "-"
        duration = detail.record.timing.get("duration_s")
        cost = detail.record.metrics.get("cost_usd")
        overlay = f"\noverlay: {detail.overlay.label} ({detail.overlay.reason})" if detail.overlay is not None else ""
        meta.update(
            "\n".join(
                [
                    detail.detail.title,
                    f"run id: {detail.record.run_id}",
                    f"status: {detail.record.status}{overlay}",
                    f"type: {detail.record.run_type}",
                    f"started: {started}",
                    f"finished: {finished}",
                    f"duration: {duration if duration is not None else '-'}",
                    f"cost: {cost if cost is not None else '-'}",
                    *detail.detail.summary_lines,
                    f"log: {detail.selected_log_path or '-'}",
                ]
            )
        )
        for artifact in detail.artifacts:
            artifacts.add_row(artifact.label, artifact.path, "yes" if artifact.exists else "no")
            self._artifact_paths.append(artifact.path)
        if self._artifact_paths:
            artifacts.cursor_type = "row"
            artifacts.move_cursor(row=min(self.state.selection.artifact_index, len(self._artifact_paths) - 1), column=0)
        actions.update(
            "\n".join(
                f"[{action.key}] {action.label}: {'enabled' if action.enabled else 'disabled'}"
                + (f" ({action.reason})" if action.reason else "")
                for action in detail.legal_actions
            )
        )

    def _render_status(self) -> None:
        query = self.state.filters.query or "-"
        self.query_one("#status-bar", Static).update(
            " | ".join(
                [
                    f"focus={self.state.focus}",
                    f"type={self.state.filters.type_filter}",
                    f"active_only={'on' if self.state.filters.active_only else 'off'}",
                    f"query={query}",
                    f"history={self.state.history_page.page + 1}/{self.state.history_page.total_pages}",
                    f"rows={self.state.live_runs.total_count} live, {self.state.history_page.total_rows} history",
                ]
            )
        )

    def _render_footer(self) -> None:
        prefix = "queue compat" if self.queue_compat else "mission control"
        follow = "follow=on" if self._follow_log else "follow=off"
        self.query_one("#footer", Static).update(
            f"{prefix} | Tab cycle panes | 1/2/3 focus | / filter | ? help | a active | t type | f outcome | o logs | s {follow}"
        )

    def _highlight_panes(self) -> None:
        for pane_name, pane_id in (("live", "#live-pane"), ("history", "#history-pane"), ("detail", "#detail-pane")):
            pane = self.query_one(pane_id)
            if pane_name == self.state.focus:
                pane.add_class("focused")
            else:
                pane.remove_class("focused")

    def _configure_tables(self) -> None:
        live = self.query_one("#live-table", DataTable)
        live.add_columns("status", "type", "id", "branch/task", "elapsed", "cost", "event")
        history = self.query_one("#history-table", DataTable)
        history.add_columns("completed", "outcome", "type", "id", "duration", "cost", "summary")
        artifacts = self.query_one("#detail-artifacts", DataTable)
        artifacts.add_columns("artifact", "path", "exists")
        self.query_one("#detail-log", Log).auto_scroll = True

    def _schedule_refresh(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._refresh_timer = self.set_timer(self.state.live_runs.refresh_interval_s, self._tick)

    def _cycle_pane(self, delta: int) -> PaneName:
        panes: list[PaneName] = ["live", "history", "detail"]
        idx = panes.index(self.state.focus)
        return panes[(idx + delta) % len(panes)]

    def _focus_pane(self, pane: PaneName) -> None:
        if pane == "detail" and self.state.focus in {"live", "history"}:
            self.state.selection.origin_pane = self.state.focus
        self.state.focus = pane
        if pane == "live":
            self.query_one("#live-table", DataTable).focus()
        elif pane == "history":
            self.query_one("#history-table", DataTable).focus()
        else:
            self.query_one("#detail-artifacts", DataTable).focus()
        self._render_status()
        self._render_footer()
        self._highlight_panes()

    def _sync_live_cursor(self) -> None:
        table = self.query_one("#live-table", DataTable)
        if not self._live_row_ids:
            return
        try:
            row = self._live_row_ids.index(self.state.selection.run_id or self._live_row_ids[0])
        except ValueError:
            row = 0
        table.move_cursor(row=row, column=0)

    def _sync_history_cursor(self) -> None:
        table = self.query_one("#history-table", DataTable)
        if not self._history_row_ids:
            return
        try:
            row = self._history_row_ids.index(self.state.selection.run_id or self._history_row_ids[0])
        except ValueError:
            row = 0
        table.move_cursor(row=row, column=0)

    def _move_live_cursor(self, delta: int) -> None:
        if not self._live_row_ids:
            return
        current = self._current_live_row()
        row = min(max(current + delta, 0), len(self._live_row_ids) - 1)
        self.query_one("#live-table", DataTable).move_cursor(row=row, column=0)
        self.state.selection.run_id = self._live_row_ids[row]
        self.state.selection.origin_pane = "live"
        self.state.selection.log_index = 0
        self.state.selection.artifact_index = 0
        self._rebuild_log_tailer()
        self._render_detail()
        self._poll_log(clear_only=True)

    def _move_history_cursor(self, delta: int) -> None:
        if not self._history_row_ids:
            return
        current = self._current_history_row()
        row = min(max(current + delta, 0), len(self._history_row_ids) - 1)
        self.query_one("#history-table", DataTable).move_cursor(row=row, column=0)
        self.state.selection.run_id = self._history_row_ids[row]
        self.state.selection.origin_pane = "history"
        self.state.selection.log_index = 0
        self.state.selection.artifact_index = 0
        self._rebuild_log_tailer()
        self._render_detail()
        self._poll_log(clear_only=True)

    def _move_artifact_cursor(self, delta: int) -> None:
        if not self._artifact_paths:
            return
        row = min(max(self.state.selection.artifact_index + delta, 0), len(self._artifact_paths) - 1)
        self.state.selection.artifact_index = row
        self.query_one("#detail-artifacts", DataTable).move_cursor(row=row, column=0)

    def _current_live_row(self) -> int:
        table = self.query_one("#live-table", DataTable)
        return min(max(table.cursor_row, 0), max(0, len(self._live_row_ids) - 1))

    def _current_history_row(self) -> int:
        table = self.query_one("#history-table", DataTable)
        return min(max(table.cursor_row, 0), max(0, len(self._history_row_ids) - 1))

    def _rebuild_log_tailer(self) -> None:
        self._log_tailer = self.model.log_tailer(self.state)

    def _poll_log(self, *, clear_only: bool) -> None:
        result = self._log_tailer.poll()
        if clear_only and not result.clear:
            return
        log_widget = self.query_one("#detail-log", Log)
        if result.clear:
            log_widget.clear()
        if result.lines:
            log_widget.write_lines(result.lines)
        log_widget.auto_scroll = self._follow_log
        if self._follow_log:
            log_widget.scroll_end(animate=False)

    def _apply_query_filter(self, value: str | None) -> None:
        if value is not None:
            self.state.filters.query = value.strip()
            self.state.filters.history_page = 0
            self._refresh_state()
        self._focus_pane(self._filter_return_pane)


__all__ = ["FilterModal", "HelpModal", "MissionControlApp"]
