from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import pyte


MOUSE_ENABLE_RE = re.compile(r"\x1b\[\?100\d+h")
MOUSE_DISABLE_RE = re.compile(r"\x1b\[\?100\d+l")
CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"


@dataclass(frozen=True)
class CastFrame:
    timestamp: float
    screen_text: str


def cast_output(path: Path) -> str:
    return "".join(event for _ts, event in _iter_output_events(path))


def mouse_enable_codes(text: str) -> list[str]:
    return MOUSE_ENABLE_RE.findall(text)


def mouse_disable_codes(text: str) -> list[str]:
    return MOUSE_DISABLE_RE.findall(text)


def render_final_frame(path: Path) -> str:
    frame = _scan_frames(path, lambda _frame: True, last=True)
    return frame.screen_text if frame is not None else ""


def find_first_frame(path: Path, predicate: Callable[[str], bool]) -> CastFrame | None:
    return _scan_frames(path, predicate, last=False)


def find_last_frame(path: Path, predicate: Callable[[str], bool]) -> CastFrame | None:
    return _scan_frames(path, predicate, last=True)


def _scan_frames(path: Path, predicate: Callable[[str], bool], *, last: bool) -> CastFrame | None:
    width, height = _read_header(path)
    fallback = _scan_frames_fallback(path, width=width, height=height, predicate=predicate, last=last)
    try:
        pyte_frame = _scan_frames_pyte(path, width=width, height=height, predicate=predicate, last=last)
    except Exception:
        return fallback
    if pyte_frame is None:
        return fallback
    if fallback is None:
        return pyte_frame
    return fallback if len(fallback.screen_text) > len(pyte_frame.screen_text) else pyte_frame


def _read_header(path: Path) -> tuple[int, int]:
    with path.open(encoding="utf-8") as handle:
        header = json.loads(handle.readline())
    return int(header.get("width", 120)), int(header.get("height", 30))


def _iter_output_events(path: Path) -> Iterator[tuple[float, str]]:
    for timestamp, event_type, data in _iter_events(path):
        if event_type == "o":
            yield timestamp, data


def _iter_events(path: Path) -> Iterator[tuple[float, str, str]]:
    with path.open(encoding="utf-8") as handle:
        next(handle)
        for line in handle:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if not isinstance(event, list) or len(event) < 3:
                continue
            yield float(event[0]), str(event[1]), str(event[2])


def _parse_resize(data: str) -> tuple[int | None, int | None]:
    match = re.fullmatch(r"(\d+)x(\d+)", data)
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _render_screen(screen: pyte.Screen) -> str:
    return "\n".join(line.rstrip() for line in screen.display).rstrip()


def _scan_frames_pyte(
    path: Path,
    *,
    width: int,
    height: int,
    predicate: Callable[[str], bool],
    last: bool,
) -> CastFrame | None:
    screen = pyte.Screen(width, height)
    stream = pyte.Stream(screen)
    matched: CastFrame | None = None
    for timestamp, event_type, data in _iter_events(path):
        if event_type == "o":
            stream.feed(data)
        elif event_type == "r":
            cols, rows = _parse_resize(data)
            if cols is not None and rows is not None:
                screen.resize(rows, cols)
            continue
        else:
            continue
        rendered = _render_screen(screen)
        if predicate(rendered):
            matched = CastFrame(timestamp=timestamp, screen_text=rendered)
            if not last:
                return matched
    return matched


def _scan_frames_fallback(
    path: Path,
    *,
    width: int,
    height: int,
    predicate: Callable[[str], bool],
    last: bool,
) -> CastFrame | None:
    screen = _MiniAnsiScreen(height, width)
    matched: CastFrame | None = None
    for timestamp, event_type, data in _iter_events(path):
        if event_type == "o":
            screen.feed(data)
        elif event_type == "r":
            cols, rows = _parse_resize(data)
            if cols is not None and rows is not None:
                screen.resize(rows, cols)
            continue
        else:
            continue
        rendered = screen.render()
        if predicate(rendered):
            matched = CastFrame(timestamp=timestamp, screen_text=rendered)
            if not last:
                return matched
    return matched


class _MiniAnsiScreen:
    def __init__(self, rows: int, cols: int) -> None:
        self.rows = rows
        self.cols = cols
        self._lines = [[" "] * cols for _ in range(rows)]
        self.row = 0
        self.col = 0
        self.saved = (0, 0)
        self.state = "normal"
        self.csi = ""
        self.osc = ""

    def resize(self, rows: int, cols: int) -> None:
        rendered = self.render().splitlines()
        self.rows = rows
        self.cols = cols
        self._lines = [[" "] * cols for _ in range(rows)]
        for row_index, line in enumerate(rendered[:rows]):
            for col_index, char in enumerate(line[:cols]):
                self._lines[row_index][col_index] = char
        self.row = min(self.row, rows - 1)
        self.col = min(self.col, cols - 1)

    def feed(self, text: str) -> None:
        for char in text:
            if self.state == "normal":
                if char == "\x1b":
                    self.state = "esc"
                elif char == "\r":
                    self.col = 0
                elif char == "\n":
                    self.row = min(self.rows - 1, self.row + 1)
                elif char == "\b":
                    self.col = max(0, self.col - 1)
                elif char == "\t":
                    self.col = min(self.cols - 1, ((self.col // 8) + 1) * 8)
                elif char >= " ":
                    self._put(char)
            elif self.state == "esc":
                if char == "[":
                    self.state = "csi"
                    self.csi = ""
                elif char == "]":
                    self.state = "osc"
                    self.osc = ""
                elif char == "7":
                    self.saved = (self.row, self.col)
                    self.state = "normal"
                elif char == "8":
                    self.row, self.col = self.saved
                    self.state = "normal"
                else:
                    self.state = "normal"
            elif self.state == "osc":
                self.osc += char
                if char == "\x07" or (char == "\\" and self.osc.endswith("\x1b\\")):
                    self.state = "normal"
            else:
                self.csi += char
                if "@" <= char <= "~":
                    self._handle_csi(self.csi)
                    self.state = "normal"

    def _put(self, char: str) -> None:
        if 0 <= self.row < self.rows and 0 <= self.col < self.cols:
            self._lines[self.row][self.col] = char
        if self.col < self.cols - 1:
            self.col += 1
        else:
            self.col = 0
            self.row = min(self.rows - 1, self.row + 1)

    def _handle_csi(self, sequence: str) -> None:
        final = sequence[-1]
        params_text = sequence[:-1]
        private = params_text.startswith("?")
        if private:
            params_text = params_text[1:]
        params = self._parse_params(params_text)
        if final in {"H", "f"}:
            row = (params[0] if len(params) >= 1 and params[0] else 1) - 1
            col = (params[1] if len(params) >= 2 and params[1] else 1) - 1
            self.row = max(0, min(self.rows - 1, row))
            self.col = max(0, min(self.cols - 1, col))
            return
        if final == "A":
            self.row = max(0, self.row - (params[0] if params else 1))
            return
        if final == "B":
            self.row = min(self.rows - 1, self.row + (params[0] if params else 1))
            return
        if final == "C":
            self.col = min(self.cols - 1, self.col + (params[0] if params else 1))
            return
        if final == "D":
            self.col = max(0, self.col - (params[0] if params else 1))
            return
        if final == "J":
            self._clear()
            return
        if final == "K":
            mode = params[0] if params else 0
            if mode == 2:
                start, stop = 0, self.cols
            elif mode == 1:
                start, stop = 0, self.col + 1
            else:
                start, stop = self.col, self.cols
            for column in range(start, stop):
                self._lines[self.row][column] = " "
            return
        if final == "m":
            return
        if private and final in {"h", "l"} and params_text == "1049":
            self._clear()

    @staticmethod
    def _parse_params(params_text: str) -> list[int]:
        if not params_text:
            return []
        out: list[int] = []
        for part in params_text.split(";"):
            match = re.match(r"\d+", part)
            out.append(int(match.group(0)) if match else 0)
        return out

    def _clear(self) -> None:
        self._lines = [[" "] * self.cols for _ in range(self.rows)]
        self.row = 0
        self.col = 0

    def render(self) -> str:
        return "\n".join("".join(line).rstrip() for line in self._lines).rstrip()
