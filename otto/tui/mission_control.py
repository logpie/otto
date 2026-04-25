"""Textual Mission Control app."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.worker import Worker, WorkerState
from textual.widgets import DataTable, Input, Log, Static

from otto.runs.registry import garbage_collect_live_records
from otto.theme import (
    MISSION_CONTROL_THEME,
    mission_control_banner_style,
    mission_control_status_style,
    mission_control_status_text,
)
from otto.mission_control.adapters import adapter_for_key
from otto.mission_control.actions import ActionResult, execute_merge_all
from otto.mission_control.model import (
    MissionControlFilters,
    MissionControlModel,
    MissionControlState,
    PaneName,
)


# Search highlighting currently depends on Textual's private Log internals.
# Keep the Textual dependency pinned to the 8.x line until this widget is
# rewritten against a public extension hook.
class SearchableLog(Log):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.search_query = ""
        self.current_match: tuple[int, int, int] | None = None
        self.match_count = 0

    def set_search(
        self,
        query: str,
        *,
        current_match: tuple[int, int, int] | None = None,
        match_count: int = 0,
    ) -> None:
        self.search_query = query
        self.current_match = current_match
        self.match_count = match_count
        self._render_line_cache.clear()
        self.refresh()

    def _render_line_strip(self, y: int, rich_style: Style) -> Strip:
        selection = self.text_selection
        if y in self._render_line_cache and selection is None:
            return self._render_line_cache[y]

        processed = self._process_line(self._lines[y])
        line_text = Text(processed, no_wrap=True)
        line_text.stylize(rich_style)
        if self.search_query:
            current = self.current_match if self.current_match and self.current_match[0] == y else None
            for start, end in _find_substring_spans(processed, self.search_query):
                style = (
                    MISSION_CONTROL_THEME.search_current
                    if current == (y, start, end)
                    else MISSION_CONTROL_THEME.search_match
                )
                line_text.stylize(style, start, end)
        if self.highlight:
            line_text = self.highlighter(line_text)
        if selection is not None and (select_span := selection.get_span(y - self._clear_y)) is not None:
            start, end = select_span
            if end == -1:
                end = len(line_text)
            selection_style = self.screen.get_component_rich_style("screen--selection")
            line_text.stylize(selection_style, start, end)

        line = Strip(line_text.render(self.app.console), cell_len(processed))
        if selection is None:
            self._render_line_cache[y] = line
        return line


class HelpModal(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("q", "close", "Close", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="help-modal"):
            yield Static("Mission Control Help", id="filter-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(_help_text(), id="help-body")
            yield Static("Esc closes. Up/Down or PageUp/PageDown scroll.", id="filter-help")

    def action_close(self) -> None:
        self.dismiss(None)


class FilterModal(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("enter", "apply", "Apply", priority=True),
    ]

    def __init__(
        self,
        initial_value: str,
        *,
        title: str = "History Filter",
        placeholder: str = "intent, branch, task id, run id",
        help_text: str = "Enter apply / Esc cancel",
    ) -> None:
        super().__init__()
        self._initial_value = initial_value
        self._title = title
        self._placeholder = placeholder
        self._help_text = help_text

    def compose(self) -> ComposeResult:
        with Container(id="filter-modal"):
            yield Static(self._title, id="filter-title")
            yield Input(
                value=self._initial_value,
                placeholder=self._placeholder,
                id="filter-input",
            )
            yield Static(self._help_text, id="filter-help")

    def on_mount(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        self.dismiss(self.query_one("#filter-input", Input).value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


class MessageModal(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", priority=True),
        Binding("enter", "close", "Close", priority=True),
        Binding("q", "close", "Close", priority=True),
    ]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        with Container(id="help-modal"):
            yield Static(self._title, id="filter-title")
            yield Static(self._message, id="help-body")

    def action_close(self) -> None:
        self.dismiss(None)


class MissionControlApp(App[int]):
    CSS = """
    Screen {
        layout: vertical;
    }

    #banner, #status-bar, #footer {
        padding: 0 1;
    }

    #banner {
        color: __BANNER_INFO__;
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
        border: round __FOCUS__;
    }

    .pane-title {
        color: __FOCUS__;
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
        color: __BANNER_SUCCESS__;
        height: auto;
    }

    #footer {
        color: __FOCUS__;
        height: auto;
    }

    #help-modal {
        width: 84;
        height: 28;
        border: round __FOCUS__;
        padding: 1 2;
        background: $surface;
    }

    #help-scroll {
        height: 1fr;
        overflow-y: auto;
    }

    #filter-modal {
        width: 60;
        height: auto;
        border: round __FOCUS__;
        padding: 1 2;
        background: $surface;
    }

    #filter-title {
        color: __FOCUS__;
        padding-bottom: 1;
    }

    #filter-help {
        color: $text-muted;
        padding-top: 1;
    }
    """.replace("__BANNER_INFO__", MISSION_CONTROL_THEME.banner_info).replace(
        "__BANNER_SUCCESS__", MISSION_CONTROL_THEME.banner_success
    ).replace("__FOCUS__", MISSION_CONTROL_THEME.focus)

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
        Binding("space", "toggle_selected", "Select", show=False, priority=True),
        Binding("enter", "open_detail", "Detail", show=False, priority=True),
        Binding("escape", "return_to_origin", "Back", show=False, priority=True),
        Binding("a", "toggle_active_only", "Active Only", show=False),
        Binding("t", "cycle_type_filter", "Type Filter", show=False),
        Binding("f", "cycle_outcome_filter", "Outcome Filter", show=False),
        Binding("/", "open_query_filter", "Query Filter", show=False),
        Binding("ctrl+f", "open_log_search", "Log Search", show=False),
        Binding("n", "log_search_next", "Next Match", show=False, priority=True),
        Binding("N", "log_search_prev", "Prev Match", show=False, priority=True),
        Binding("[", "history_prev_page", "Prev Page", show=False, priority=True),
        Binding("]", "history_next_page", "Next Page", show=False, priority=True),
        Binding("pageup", "history_prev_page", "Prev Page", show=False, priority=True),
        Binding("pagedown", "history_next_page", "Next Page", show=False, priority=True),
        Binding("o", "cycle_logs", "Cycle Logs", show=False),
        Binding("s", "toggle_follow", "Toggle Follow", show=False),
        Binding("home", "log_top", "Log Top", show=False),
        Binding("end", "log_bottom", "Log Bottom", show=False),
        Binding("c", "invoke_action('c')", "Cancel", show=False),
        Binding("r", "invoke_action('r')", "Resume", show=False),
        Binding("R", "invoke_action('R')", "Retry", show=False),
        Binding("x", "invoke_action('x')", "Cleanup", show=False),
        Binding("m", "invoke_action('m')", "Merge", show=False),
        Binding("M", "merge_all", "Merge All", show=False),
        Binding("e", "invoke_action('e')", "Edit", show=False),
        Binding("y", "yank", "Copy", show=False),
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
        self._action_banner: str | None = None
        self._banner_severity = "info"
        self._log_search_query = ""
        self._log_search_match_index = -1
        self._log_search_match_total = 0

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
                yield SearchableLog(id="detail-log")
        yield Static("", id="status-bar")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        garbage_collect_live_records(self.project_dir)
        self.state = self.model.refresh(self.state)
        self._configure_tables()
        self._render_state()
        self._poll_log(clear_only=False)
        self._schedule_refresh()
        self._focus_pane(self.state.focus)

    def action_quit(self) -> None:
        self.exit(0)

    def on_unmount(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

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
        if self.state.focus == "detail":
            self.action_invoke_action("e")
            return
        self._focus_pane("detail")

    def action_toggle_selected(self) -> None:
        run_id = self.state.selection.run_id
        if run_id is None:
            return
        live_ids = {item.record.run_id for item in self.state.live_runs.items}
        if run_id not in live_ids:
            return
        if run_id in self.state.selected_run_ids:
            self.state.selected_run_ids.remove(run_id)
        else:
            self.state.selected_run_ids.add(run_id)
        self._render_status()
        self._render_footer()

    def action_return_to_origin(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss(None)
            return
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
        if self.state.focus == "detail":
            self.action_open_log_search()
            return
        self._filter_return_pane = self.state.focus if self.state.focus in {"live", "history"} else self.state.selection.origin_pane
        self.push_screen(FilterModal(self.state.filters.query), self._apply_query_filter)

    def action_open_log_search(self) -> None:
        if self.state.focus != "detail":
            return
        self._filter_return_pane = "detail"
        self.push_screen(
            FilterModal(
                self._log_search_query,
                title="Log Search",
                placeholder="substring search in current log",
                help_text="Enter apply / Esc cancel / n next / N previous",
            ),
            self._apply_log_search,
        )

    def action_history_prev_page(self) -> None:
        if self.state.focus != "history":
            return
        self.model.previous_history_page(self.state)
        self._refresh_state()

    def action_history_next_page(self) -> None:
        if self.state.focus != "history":
            return
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
        log_widget = self.query_one("#detail-log", SearchableLog)
        log_widget.auto_scroll = True
        log_widget.scroll_end(animate=False)
        self._render_footer()

    def action_log_search_next(self) -> None:
        self._advance_log_search(1)

    def action_log_search_prev(self) -> None:
        self._advance_log_search(-1)

    def action_invoke_action(self, key: str) -> None:
        detail = self.model.detail_view(self.state)
        if detail is None:
            self.notify("no selection", severity="warning")
            return
        if key == "m" and self.state.selected_run_ids:
            selected_queue_task_ids = self._selected_queue_task_ids(detail, key)
            if not selected_queue_task_ids:
                message = "no selected done queue rows"
                self._action_banner = message
                self._banner_severity = "warning"
                self._render_banner()
                self.notify(message, severity="warning")
                return
            self._action_banner = "merge selected requested..."
            self._banner_severity = "info"
            self._render_banner()
            self.run_worker(
                lambda: self._execute_detail_action(
                    detail.record,
                    key,
                    selected_queue_task_ids=selected_queue_task_ids,
                ),
                name=f"mission-control-action:{key}",
                group="mission-control-actions",
                thread=True,
                exit_on_error=False,
            )
            return
        for action in detail.legal_actions:
            if action.key == key:
                if not action.enabled:
                    message = action.reason or action.preview
                    self._action_banner = message
                    self._banner_severity = "warning"
                    self._render_banner()
                    self.notify(message, severity="warning")
                    return
                self._action_banner = f"{action.label} requested..."
                self._banner_severity = "info"
                self._render_banner()
                self.run_worker(
                    lambda: self._execute_detail_action(
                        detail.record,
                        action.key,
                        selected_artifact_path=self._selected_artifact_path(detail) if action.key == "e" else None,
                        selected_queue_task_ids=self._selected_queue_task_ids(detail, action.key),
                    ),
                    name=f"mission-control-action:{action.key}",
                    group="mission-control-actions",
                    thread=True,
                    exit_on_error=False,
                )
                return
        self.notify("action unavailable", severity="warning")

    def action_merge_all(self) -> None:
        self._action_banner = "merge all requested..."
        self._banner_severity = "info"
        self._render_banner()
        self.run_worker(
            self._execute_merge_all,
            name="mission-control-action:M",
            group="mission-control-actions",
            thread=True,
            exit_on_error=False,
        )

    def action_yank(self) -> None:
        payload = self._clipboard_payload()
        if not payload:
            self.notify("nothing to copy", severity="warning")
            return
        ok, message = _copy_to_clipboard(payload)
        self._action_banner = message
        self._banner_severity = "success" if ok else "warning"
        self._render_banner()
        self.notify(message, severity="information" if ok else "warning")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if not event.worker.name.startswith("mission-control-action:"):
            return
        if event.state == WorkerState.SUCCESS and isinstance(event.worker.result, ActionResult):
            self._handle_action_result(event.worker.result)
            return
        if event.state == WorkerState.ERROR:
            message = str(event.worker.error or "worker failed")
            self._handle_action_result(
                ActionResult(
                    ok=False,
                    severity="error",
                    modal_title="Action failed",
                    modal_message=message,
                    message=message,
                )
            )

    def _tick(self) -> None:
        try:
            self._refresh_state()
        except NoMatches:
            if self._refresh_timer is not None:
                self._refresh_timer.stop()
                self._refresh_timer = None

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
        banner = self._action_banner or self.state.last_event_banner or ""
        if not banner:
            self.query_one("#banner", Static).update("")
            return
        severity = self._banner_severity if self._action_banner else "info"
        self.query_one("#banner", Static).update(Text(banner, style=mission_control_banner_style(severity)))

    def _render_live(self) -> None:
        table = self.query_one("#live-table", DataTable)
        table.clear(columns=False)
        self._live_row_ids = []
        for item in self.state.live_runs.items:
            status = item.overlay.label if item.overlay is not None else item.record.status.upper()
            table.add_row(
                mission_control_status_text(
                    status,
                    status=item.record.status,
                    overlay=item.overlay.level if item.overlay is not None else None,
                ),
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
                mission_control_status_text(item.outcome_display, status=row.status),
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
            if self.state.live_runs.total_count == 0 and self.state.history_page.total_rows == 0:
                meta.update(_empty_state_text())
            else:
                meta.update("No selection.")
            actions.update("")
            self.query_one("#detail-log", SearchableLog).clear()
            return

        started = detail.record.timing.get("started_at") or "-"
        finished = detail.record.timing.get("finished_at") or "-"
        duration = detail.record.timing.get("duration_s")
        cost = detail.record.metrics.get("cost_usd")
        meta_text = Text()
        meta_text.append(f"{detail.detail.title}\n")
        meta_text.append(f"run id: {detail.record.run_id}\n")
        meta_text.append("status: ")
        meta_text.append(
            detail.overlay.label if detail.overlay is not None else detail.record.status,
            style=mission_control_status_style(
                detail.record.status,
                overlay=detail.overlay.level if detail.overlay is not None else None,
            ),
        )
        if detail.overlay is not None:
            meta_text.append(f" ({detail.overlay.reason})")
        meta_text.append("\n")
        meta_text.append(f"type: {detail.record.run_type}\n")
        branch = str(detail.record.git.get("branch") or "").strip()
        worktree = str(detail.record.git.get("worktree") or "").strip()
        if branch:
            meta_text.append(f"branch: {branch}\n")
        if worktree:
            meta_text.append(f"worktree: {worktree}\n")
        meta_text.append("log:\n")
        for path_line in _detail_path_lines(detail.selected_log_path or "-"):
            meta_text.append(f"{path_line}\n")
        if detail.artifacts:
            meta_text.append("\nartifacts:\n")
            for artifact in detail.artifacts:
                meta_text.append(f"{artifact.label}:\n")
                for path_line in _detail_path_lines(artifact.path):
                    meta_text.append(f"{path_line}\n")
        meta_text.append(f"started: {started}\n")
        meta_text.append(f"finished: {finished}\n")
        meta_text.append(f"duration: {duration if duration is not None else '-'}\n")
        meta_text.append(f"cost: {cost if cost is not None else '-'}\n")
        for line in detail.detail.summary_lines:
            meta_text.append(f"{line}\n")
        meta.update(meta_text)
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
        log_search = "-"
        if self._log_search_query:
            if self._log_search_match_total:
                log_search = f"{self._log_search_query} ({self._log_search_match_index + 1}/{self._log_search_match_total})"
            else:
                log_search = f"{self._log_search_query} (0/0)"
        self.query_one("#status-bar", Static).update(
            " | ".join(
                [
                    f"focus={self.state.focus}",
                    f"type={self.state.filters.type_filter}",
                    f"active_only={'on' if self.state.filters.active_only else 'off'}",
                    f"selected={len(self.state.selected_run_ids)}",
                    f"query={query}",
                    f"log_search={log_search}",
                    f"history={self.state.history_page.page + 1}/{self.state.history_page.total_pages}",
                    f"rows={self.state.live_runs.total_count} live, {self.state.history_page.total_rows} history",
                ]
            )
        )

    def _render_footer(self) -> None:
        prefix = "queue compat" if self.queue_compat else "mission control"
        follow = "follow=on" if self._follow_log else "follow=off"
        if self.state.focus == "live":
            hint = "j/k move | space select | Enter detail | a active | t type | ? help"
        elif self.state.focus == "history":
            hint = "j/k move | / filter | f outcome | [ ] page | Enter detail | ? help"
        else:
            search_hint = "n next | N prev | " if self._log_search_query else ""
            hint = f"/ or Ctrl-F search log | {search_hint}o logs | s {follow} | Home/End scroll | e open | Esc back | ? help"
        self.query_one("#footer", Static).update(f"{prefix} | {hint}")

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
        self.query_one("#detail-log", SearchableLog).auto_scroll = True

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
        self._log_search_match_index = -1

    def _poll_log(self, *, clear_only: bool) -> None:
        result = self._log_tailer.poll()
        if clear_only and not result.clear:
            return
        log_widget = self.query_one("#detail-log", SearchableLog)
        if result.clear:
            log_widget.clear()
        if result.lines:
            log_widget.write_lines(result.lines)
        if self._log_search_query:
            self._sync_log_search_highlighting(announce=False)
        log_widget.auto_scroll = self._follow_log
        if self._follow_log:
            log_widget.scroll_end(animate=False)

    def _apply_query_filter(self, value: str | None) -> None:
        if value is not None:
            self.state.filters.query = value.strip()
            self.state.filters.history_page = 0
            self._refresh_state()
        self._focus_pane(self._filter_return_pane)

    def _apply_log_search(self, value: str | None) -> None:
        if value is not None:
            self._log_search_query = value.strip()
            self._log_search_match_index = -1
            self._sync_log_search_highlighting(announce=bool(self._log_search_query))
            if self._log_search_match_total:
                self._jump_to_current_log_match()
        self._focus_pane(self._filter_return_pane)

    def _execute_detail_action(
        self,
        record,
        action_kind: str,
        *,
        selected_artifact_path: str | None = None,
        selected_queue_task_ids: list[str] | None = None,
    ) -> ActionResult:
        adapter = adapter_for_key(record.adapter_key)
        return adapter.execute(
            record,
            action_kind,
            self.project_dir,
            selected_artifact_path=selected_artifact_path,
            selected_queue_task_ids=selected_queue_task_ids,
            post_result=lambda result: self.call_from_thread(self._handle_action_result, result),
        )

    def _execute_merge_all(self) -> ActionResult:
        return execute_merge_all(
            self.project_dir,
            post_result=lambda result: self.call_from_thread(self._handle_action_result, result),
        )

    def _handle_action_result(self, result: ActionResult) -> None:
        if result.clear_banner:
            self._action_banner = None
        elif result.message:
            self._action_banner = result.message
            self._banner_severity = result.severity or ("success" if result.ok else "error")
        self._render_banner()
        if result.message:
            self.notify(result.message, severity=result.severity)
        if result.modal_title and result.modal_message:
            self.push_screen(MessageModal(result.modal_title, result.modal_message))
        if result.refresh:
            self._refresh_state()

    def _selected_artifact_path(self, detail) -> str | None:
        if not detail.artifacts:
            return None
        index = min(max(self.state.selection.artifact_index, 0), len(detail.artifacts) - 1)
        return detail.artifacts[index].path

    def _selected_queue_task_ids(self, detail, key: str) -> list[str] | None:
        if key != "m":
            return None
        if self.state.selected_run_ids:
            task_ids = [
                str(item.record.identity.get("queue_task_id"))
                for item in self.state.live_runs.items
                if item.record.run_id in self.state.selected_run_ids
                and item.record.domain == "queue"
                and item.record.status == "done"
                and item.record.identity.get("queue_task_id")
            ]
            if task_ids:
                return task_ids
        task_id = detail.record.identity.get("queue_task_id")
        return [str(task_id)] if task_id else None

    def _clipboard_payload(self) -> str | None:
        detail = self.model.detail_view(self.state)
        if detail is None:
            return None
        if self.state.focus == "detail" and detail.selected_log_path:
            try:
                return Path(detail.selected_log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        return _detail_clipboard_payload(detail)

    def _advance_log_search(self, delta: int) -> None:
        if self.state.focus != "detail":
            return
        if not self._log_search_query:
            self.notify("no active log search", severity="warning")
            return
        matches = self._current_log_matches()
        if not matches:
            self.notify("no log matches", severity="warning")
            return
        if self._log_search_match_index < 0:
            self._log_search_match_index = 0 if delta > 0 else len(matches) - 1
        else:
            self._log_search_match_index = (self._log_search_match_index + delta) % len(matches)
        self.query_one("#detail-log", SearchableLog).set_search(
            self._log_search_query,
            current_match=matches[self._log_search_match_index],
            match_count=len(matches),
        )
        self._log_search_match_total = len(matches)
        self._jump_to_current_log_match()

    def _sync_log_search_highlighting(self, *, announce: bool) -> None:
        log_widget = self.query_one("#detail-log", SearchableLog)
        if not self._log_search_query:
            self._log_search_match_index = -1
            self._log_search_match_total = 0
            log_widget.set_search("", current_match=None, match_count=0)
            self._render_status()
            self._render_footer()
            return
        matches = self._current_log_matches()
        if not matches:
            self._log_search_match_index = -1
            self._log_search_match_total = 0
            log_widget.set_search(self._log_search_query, current_match=None, match_count=0)
            self._render_status()
            self._render_footer()
            if announce:
                self.notify("no log matches", severity="warning")
            return
        if self._log_search_match_index < 0 or self._log_search_match_index >= len(matches):
            self._log_search_match_index = 0
        self._log_search_match_total = len(matches)
        log_widget.set_search(
            self._log_search_query,
            current_match=matches[self._log_search_match_index],
            match_count=len(matches),
        )
        self._render_status()
        self._render_footer()

    def _jump_to_current_log_match(self) -> None:
        matches = self._current_log_matches()
        if not matches or self._log_search_match_index < 0:
            return
        line, _start, _end = matches[self._log_search_match_index]
        self._follow_log = False
        log_widget = self.query_one("#detail-log", SearchableLog)
        log_widget.auto_scroll = False
        log_widget.scroll_to(y=max(0, line - 2), animate=False, force=True, immediate=True)
        self._render_status()
        self._render_footer()

    def _current_log_matches(self) -> list[tuple[int, int, int]]:
        lines = self.query_one("#detail-log", SearchableLog).lines
        matches: list[tuple[int, int, int]] = []
        for line_number, line in enumerate(lines):
            matches.extend((line_number, start, end) for start, end in _find_substring_spans(line, self._log_search_query))
        return matches


__all__ = ["FilterModal", "HelpModal", "MessageModal", "MissionControlApp"]


def _help_text() -> str:
    return "\n".join(
        [
            "Panes",
            "Live Runs shows active and recently finished live registry rows across build, improve, certify, merge, and queue.",
            "History shows terminal snapshots and older rows with paging, substring filtering, and outcome/type filters.",
            "Detail + Logs shows metadata, artifacts, legal actions, and the currently selected log stream for the selected row.",
            "",
            "Navigation",
            "Tab / Shift-Tab cycle panes.",
            "1 / 2 / 3 focus Live / History / Detail.",
            "j / k or Up / Down move the current row or artifact selection.",
            "Enter pins the current row and focuses Detail.",
            "Esc returns from Detail to the pane where the selection came from.",
            "q quits the app. ? opens this help.",
            "",
            "Filters And Selection",
            "a toggles active-only live rows.",
            "t cycles the run type filter: all, build, improve, certify, merge, queue.",
            "f cycles the history outcome filter: all, success, failed, interrupted, cancelled, removed, other.",
            "/ opens the history substring filter from Live or History. Matches run id, task id, branch, and intent.",
            "Space toggles multi-select on the current live row. Multi-select is used for queue merge-on-done flows.",
            "[ and ] move history pages when History has focus.",
            "",
            "Detail And Logs",
            "o cycles between available log files for the selected run.",
            "s toggles log follow mode.",
            "Home jumps to the top of the current log. End resumes follow and jumps to the bottom.",
            "/ or Ctrl-F opens substring search for the current log when Detail has focus.",
            "n jumps to the next log search match. N jumps to the previous match.",
            "Log search highlights every match and uses a stronger highlight for the current match.",
            "y copies the selected row metadata from Live/History, or the current log from Detail.",
            "",
            "Actions",
            "c cancel, r resume, R retry, x cleanup, m merge selected queue item(s), M merge all done queue items, e open the selected artifact or log.",
            "Disabled actions stay visible with a reason so compatibility and capability limits are explicit.",
            "",
            "Status Codes",
            "RUNNING / STARTING: active work in progress.",
            "QUEUED / PAUSED: waiting state managed by queue or workflow control.",
            "DONE: terminal success.",
            "FAILED: terminal failure.",
            "INTERRUPTED: stopped before completion but may be resumable.",
            "CANCELLED: stopped intentionally.",
            "REMOVED: terminal row retained briefly before cleanup.",
            "LAGGING: heartbeat overdue, but the writer still appears alive or the reader is in a grace window.",
            "STALE: heartbeat stalled and the writer identity is gone.",
            "",
            "Compatibility Notes",
            "queue compat mode keeps `otto queue dashboard` muscle memory by opening Mission Control filtered to queue runs.",
            "Legacy queue rows say `legacy queue mode` and disable registry-backed logs or artifact actions that old watchers cannot provide.",
        ]
    )


def _empty_state_text() -> str:
    return "\n".join(
        [
            "No runs yet.",
            "",
            'Start with `otto build "..."` for new product work.',
            'Use `otto improve bugs "..."` for existing defects.',
            "Run `otto certify` to inspect an existing app.",
            'Queue parallel work with `otto queue build <task-id> "..."`.',
        ]
    )


def _detail_path_lines(path: str, *, max_width: int = 34) -> list[str]:
    if len(path) <= max_width:
        return [path]
    parts = Path(path).parts
    if not parts:
        return [path]
    lines: list[str] = []
    current = parts[0]
    for part in parts[1:]:
        separator = "" if current.endswith("/") else "/"
        candidate = f"{current}{separator}{part}" if current else part
        if current and len(candidate) > max_width:
            lines.append(current)
            current = f"/{part}" if path.startswith("/") else part
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _find_substring_spans(text: str, query: str) -> list[tuple[int, int]]:
    needle = query.casefold().strip()
    if not needle:
        return []
    haystack = text.casefold()
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return spans
        spans.append((index, index + len(needle)))
        start = index + len(needle)


def _copy_to_clipboard(text: str) -> tuple[bool, str]:
    commands = (
        ("pbcopy",),
        ("wl-copy",),
        ("xclip", "-selection", "clipboard"),
    )
    for command in commands:
        executable = shutil.which(command[0])
        if executable is None:
            continue
        try:
            result = subprocess.run(
                [executable, *command[1:]],
                input=text,
                text=True,
                capture_output=True,
                timeout=3.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"clipboard copy failed: {exc}"
        if result.returncode == 0:
            return True, "copied to clipboard"
        stderr = (result.stderr or result.stdout or "").strip()
        return False, f"clipboard copy failed: {stderr or command[0]}"
    return False, "clipboard not available"


def _detail_clipboard_payload(detail) -> str:
    record = detail.record
    lines = [
        detail.detail.title,
        f"run_id: {record.run_id}",
        f"status: {record.status}",
        f"type: {record.run_type}",
        f"domain: {record.domain}",
        f"command: {record.command}",
        f"display_name: {record.display_name}",
        f"project_dir: {record.project_dir}",
        f"cwd: {record.cwd}",
        f"last_event: {record.last_event}",
    ]
    if record.terminal_outcome:
        lines.append(f"terminal_outcome: {record.terminal_outcome}")
    for section_name, section in (
        ("identity", record.identity),
        ("source", record.source),
        ("timing", record.timing),
        ("git", record.git),
        ("intent", record.intent),
        ("metrics", record.metrics),
    ):
        if not isinstance(section, dict) or not section:
            continue
        for key in sorted(section):
            value = section.get(key)
            if value is not None and value != "":
                lines.append(f"{section_name}.{key}: {value}")
    for line in detail.detail.summary_lines:
        lines.append(line)
    for artifact in detail.artifacts:
        lines.append(f"artifact.{artifact.label}: {artifact.path}")
        lines.append(f"artifact.{artifact.label}.exists: {'yes' if artifact.exists else 'no'}")
    if detail.selected_log_path:
        lines.append(f"selected_log: {detail.selected_log_path}")
    return "\n".join(lines).rstrip() + "\n"
