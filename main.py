"""
PyQt5-based dashboard alarm for Raspberry Pi.
- Shows current time in a chosen timezone.
- Lets the user set an alarm time that toggles a GPIO pin when reached.
- Fetches today's Todoist tasks via the REST API.
- Fetches today's Google Calendar events using the Calendar API.

Environment variables:
- TODOIST_API_TOKEN: Token for the Todoist REST API.
- GOOGLE_SERVICE_ACCOUNT_FILE: Path to a Google service account JSON for Calendar access.
- GOOGLE_CALENDAR_ID: ID of the calendar to query.

The application uses dark styling for a modern appearance suitable for a small touchscreen.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import requests
from PyQt5 import QtCore, QtGui, QtWidgets

# GPIO handling -----------------------------------------------------------------
GPIO_AVAILABLE = bool(importlib.util.find_spec("RPi")) and bool(
    importlib.util.find_spec("RPi.GPIO")
)

if GPIO_AVAILABLE:
    import RPi.GPIO as GPIO
else:
    class MockGPIO:
        BCM = "BCM"
        OUT = "OUT"

        def __init__(self) -> None:
            self.state = {}

        def setmode(self, mode: str) -> None:  # pragma: no cover - hardware stub
            print(f"[MockGPIO] setmode({mode})")

        def setup(self, pin: int, mode: str) -> None:  # pragma: no cover
            print(f"[MockGPIO] setup(pin={pin}, mode={mode})")
            self.state[pin] = 0

        def output(self, pin: int, value: bool) -> None:  # pragma: no cover
            print(f"[MockGPIO] output(pin={pin}, value={value})")
            self.state[pin] = 1 if value else 0

        def cleanup(self) -> None:  # pragma: no cover
            print("[MockGPIO] cleanup()")
            self.state.clear()

    GPIO = MockGPIO()


@dataclass
class TodoItem:
    content: str
    due: Optional[str] = None


@dataclass
class CalendarEvent:
    summary: str
    start: datetime
    end: datetime


class AlarmController(QtCore.QObject):
    alarm_triggered = QtCore.pyqtSignal()

    def __init__(self, pin: int, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self.pin = pin
        self.active_time: Optional[time] = None
        self.alarm_active = False

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pin, GPIO.OUT)
        GPIO.output(self.pin, False)

    def set_alarm(self, alarm_time: time) -> None:
        self.active_time = alarm_time
        self.alarm_active = False

    def clear_alarm(self) -> None:
        self.active_time = None
        self.alarm_active = False
        GPIO.output(self.pin, False)

    def check_alarm(self, current_time: datetime) -> None:
        if self.active_time is None:
            return

        if self.alarm_active:
            return

        if (
            current_time.hour == self.active_time.hour
            and current_time.minute == self.active_time.minute
        ):
            self.alarm_active = True
            GPIO.output(self.pin, True)
            self.alarm_triggered.emit()
            QtCore.QTimer.singleShot(30000, self.stop_alarm)

    def stop_alarm(self) -> None:
        GPIO.output(self.pin, False)


class TodoistClient:
    API_URL = "https://api.todoist.com/rest/v2/tasks"

    def __init__(self, token: Optional[str]) -> None:
        self.token = token

    def fetch_today(self) -> List[TodoItem]:
        if not self.token:
            return [TodoItem("TODOIST_API_TOKEN nicht gesetzt")]

        headers = {"Authorization": f"Bearer {self.token}"}
        params = {"filter": "today"}
        response = requests.get(self.API_URL, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        items: List[TodoItem] = []
        for item in data:
            due_str = None
            if item.get("due") and item["due"].get("datetime"):
                due_dt = datetime.fromisoformat(item["due"]["datetime"])
                due_str = due_dt.strftime("%H:%M")
            items.append(TodoItem(content=item.get("content", ""), due=due_str))
        return items


class GoogleCalendarClient:
    def __init__(self, credentials_file: Optional[str], calendar_id: Optional[str]) -> None:
        self.credentials_file = credentials_file
        self.calendar_id = calendar_id

        self.api_available = bool(importlib.util.find_spec("googleapiclient")) and bool(
            importlib.util.find_spec("google.oauth2")
        )

    def fetch_today(self) -> List[CalendarEvent]:
        if not self.api_available:
            return [
                CalendarEvent(
                    summary="Google API Bibliothek nicht installiert",
                    start=datetime.now(timezone.utc),
                    end=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            ]

        if not self.credentials_file or not self.calendar_id:
            return [
                CalendarEvent(
                    summary="GOOGLE_SERVICE_ACCOUNT_FILE oder GOOGLE_CALENDAR_ID fehlt",
                    start=datetime.now(timezone.utc),
                    end=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            ]

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
        credentials = service_account.Credentials.from_service_account_file(
            self.credentials_file, scopes=scopes
        )
        service = build("calendar", "v3", credentials=credentials)

        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        end_of_day = start_of_day + timedelta(days=1)

        events_result = (
            service.events()
            .list(
                calendarId=self.calendar_id,
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])

        parsed: List[CalendarEvent] = []
        for event in events:
            start_str = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            end_str = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
            if not start_str or not end_str:
                continue

            start_dt = self._parse_iso_time(start_str)
            end_dt = self._parse_iso_time(end_str)
            parsed.append(
                CalendarEvent(
                    summary=event.get("summary", "Ohne Titel"),
                    start=start_dt,
                    end=end_dt,
                )
            )
        return parsed

    @staticmethod
    def _parse_iso_time(value: str) -> datetime:
        if "T" in value:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("Europe/Berlin"))
            return dt
        return datetime.fromisoformat(value + "T00:00:00+00:00")


class TouchTimeDialog(QtWidgets.QDialog):
    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Touch Zeit wählen")
        self.setModal(True)
        self.digits: str = ""
        self._selected: Optional[QtCore.QTime] = None

        layout = QtWidgets.QVBoxLayout()
        self.display = QtWidgets.QLabel(self._format_display())
        self.display.setAlignment(QtCore.Qt.AlignCenter)
        self.display.setFont(QtGui.QFont("Montserrat", 28, QtGui.QFont.Bold))
        layout.addWidget(self.display)

        grid = QtWidgets.QGridLayout()
        buttons = [str(i) for i in range(1, 10)] + ["C", "0", "←"]
        for idx, text in enumerate(buttons):
            btn = QtWidgets.QPushButton(text)
            btn.setMinimumSize(80, 80)
            if text.isdigit():
                btn.clicked.connect(lambda _, t=text: self._append_digit(t))
            elif text == "C":
                btn.clicked.connect(self._clear_digits)
            else:
                btn.clicked.connect(self._backspace)
            row, col = divmod(idx, 3)
            grid.addWidget(btn, row, col)
        layout.addLayout(grid)

        action_row = QtWidgets.QHBoxLayout()
        ok_btn = QtWidgets.QPushButton("OK")
        ok_btn.setMinimumHeight(60)
        ok_btn.clicked.connect(self._accept_time)
        cancel_btn = QtWidgets.QPushButton("Abbrechen")
        cancel_btn.setMinimumHeight(60)
        cancel_btn.clicked.connect(self.reject)
        action_row.addWidget(ok_btn)
        action_row.addWidget(cancel_btn)
        layout.addLayout(action_row)

        self.setLayout(layout)

    def _format_display(self) -> str:
        padded = self.digits.ljust(4, "_")
        return f"{padded[:2]}:{padded[2:]}"

    def _append_digit(self, digit: str) -> None:
        if len(self.digits) >= 4:
            return
        self.digits += digit
        self.display.setText(self._format_display())

    def _backspace(self) -> None:
        self.digits = self.digits[:-1]
        self.display.setText(self._format_display())

    def _clear_digits(self) -> None:
        self.digits = ""
        self.display.setText(self._format_display())

    def _accept_time(self) -> None:
        if len(self.digits) != 4:
            QtWidgets.QMessageBox.warning(self, "Ungültig", "Bitte vier Ziffern eingeben (HHMM).")
            return
        hours = int(self.digits[:2])
        minutes = int(self.digits[2:])
        if not (0 <= hours <= 23 and 0 <= minutes <= 59):
            QtWidgets.QMessageBox.warning(self, "Ungültig", "Bitte gültige Uhrzeit eingeben.")
            return
        self._selected = QtCore.QTime(hours, minutes)
        self.accept()

    def selected_time(self) -> Optional[QtCore.QTime]:
        return self._selected


class AlarmFavorites:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or Path.home() / ".dashboard_alarm_favorites.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.favorites: List[Optional[str]] = [None, None]
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            favs = data.get("favorites", [])
            for idx in range(min(2, len(favs))):
                if isinstance(favs[idx], str):
                    self.favorites[idx] = favs[idx]
        except (OSError, json.JSONDecodeError):
            self.favorites = [None, None]

    def _save(self) -> None:
        payload = {"favorites": self.favorites}
        try:
            self.path.write_text(json.dumps(payload, indent=2))
        except OSError:
            pass

    def set_favorite(self, index: int, value: QtCore.QTime) -> None:
        if index not in (0, 1):
            return
        self.favorites[index] = value.toString("HH:mm")
        self._save()

    def get_favorite(self, index: int) -> Optional[QtCore.QTime]:
        if index not in (0, 1):
            return None
        value = self.favorites[index]
        if not value:
            return None
        time_val = QtCore.QTime.fromString(value, "HH:mm")
        return time_val if time_val.isValid() else None


class DashboardWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Raspberry Pi Tagesdashboard")
        self.resize(600, 800)

        self.todo_client = TodoistClient(os.getenv("TODOIST_API_TOKEN"))
        self.calendar_client = GoogleCalendarClient(
            os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"), os.getenv("GOOGLE_CALENDAR_ID")
        )
        self.alarm = AlarmController(pin=18)
        self.alarm.alarm_triggered.connect(self._on_alarm_triggered)
        self.favorites = AlarmFavorites()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        self.clock_label = QtWidgets.QLabel("00:00:00")
        self.clock_label.setAlignment(QtCore.Qt.AlignCenter)
        self.clock_label.setFont(QtGui.QFont("Montserrat", 36, QtGui.QFont.Bold))

        self.timezone_label = QtWidgets.QLabel("Uhrzeit - GMT")
        self.timezone_label.setAlignment(QtCore.Qt.AlignCenter)

        clock_container = QtWidgets.QVBoxLayout()
        clock_container.addWidget(self.timezone_label)
        clock_container.addWidget(self.clock_label)

        alarm_group = QtWidgets.QGroupBox("Wecker Uhrzeit")
        alarm_layout = QtWidgets.QVBoxLayout()
        alarm_row = QtWidgets.QHBoxLayout()
        self.alarm_time_edit = QtWidgets.QTimeEdit(QtCore.QTime.currentTime())
        self.alarm_time_edit.setDisplayFormat("HH:mm")
        self.alarm_time_edit.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.touch_time_button = QtWidgets.QPushButton("Touch Eingabe")
        self.touch_time_button.clicked.connect(self._open_touch_time_dialog)
        self.alarm_button = QtWidgets.QPushButton("Wecker setzen")
        self.alarm_button.clicked.connect(self._set_alarm)
        self.clear_alarm_button = QtWidgets.QPushButton("Wecker stoppen")
        self.clear_alarm_button.clicked.connect(self._clear_alarm)
        alarm_row.addWidget(self.alarm_time_edit)
        alarm_row.addWidget(self.touch_time_button)
        alarm_row.addWidget(self.alarm_button)
        alarm_row.addWidget(self.clear_alarm_button)

        favorites_row = QtWidgets.QHBoxLayout()
        self.favorite_buttons: List[QtWidgets.QPushButton] = []
        self.favorite_save_buttons: List[QtWidgets.QPushButton] = []
        for idx in range(2):
            apply_button = QtWidgets.QPushButton(f"Favorit {idx + 1}")
            apply_button.clicked.connect(lambda _, i=idx: self._apply_favorite(i))
            save_button = QtWidgets.QPushButton(f"Favorit {idx + 1} speichern")
            save_button.clicked.connect(lambda _, i=idx: self._save_favorite(i))
            self.favorite_buttons.append(apply_button)
            self.favorite_save_buttons.append(save_button)
            favorites_row.addWidget(apply_button)
            favorites_row.addWidget(save_button)

        alarm_layout.addLayout(alarm_row)
        alarm_layout.addLayout(favorites_row)
        alarm_group.setLayout(alarm_layout)

        todo_group = QtWidgets.QGroupBox("Todoist - Heutige Aufgaben")
        todo_layout = QtWidgets.QVBoxLayout()
        self.todo_list = QtWidgets.QListWidget()
        todo_layout.addWidget(self.todo_list)
        todo_group.setLayout(todo_layout)

        calendar_group = QtWidgets.QGroupBox("Google Calendar Termine für den heutigen Tag")
        calendar_layout = QtWidgets.QVBoxLayout()
        self.calendar_list = QtWidgets.QListWidget()
        calendar_layout.addWidget(self.calendar_list)
        calendar_group.setLayout(calendar_layout)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(clock_container)
        layout.addWidget(alarm_group)
        layout.addWidget(todo_group)
        layout.addWidget(calendar_group)
        layout.addStretch(1)
        central.setLayout(layout)

        self._apply_dark_style()
        self._load_favorites()
        self._start_timers()
        self.refresh_data()

    def _open_touch_time_dialog(self) -> None:
        dialog = TouchTimeDialog(self)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            selected_time = dialog.selected_time()
            if selected_time is not None:
                self.alarm_time_edit.setTime(selected_time)

    def _apply_dark_style(self) -> None:
        dark_palette = QtGui.QPalette()
        dark_palette.setColor(QtGui.QPalette.Window, QtGui.QColor(30, 30, 30))
        dark_palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(220, 220, 220))
        dark_palette.setColor(QtGui.QPalette.Base, QtGui.QColor(45, 45, 45))
        dark_palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(35, 35, 35))
        dark_palette.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor(220, 220, 220))
        dark_palette.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor(220, 220, 220))
        dark_palette.setColor(QtGui.QPalette.Text, QtGui.QColor(220, 220, 220))
        dark_palette.setColor(QtGui.QPalette.Button, QtGui.QColor(45, 45, 45))
        dark_palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(220, 220, 220))
        dark_palette.setColor(QtGui.QPalette.BrightText, QtCore.Qt.red)
        dark_palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(64, 128, 255))
        dark_palette.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
        self.setPalette(dark_palette)

        self.setStyleSheet(
            """
            QWidget {
                font-family: 'Montserrat', 'Helvetica Neue', sans-serif;
                font-size: 16px;
                color: #e0e0e0;
            }
            QGroupBox {
                border: 1px solid #444;
                border-radius: 8px;
                margin-top: 12px;
                padding: 12px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px 0 3px;
            }
            QPushButton {
                background-color: #3a3a3a;
                border: 1px solid #555;
                border-radius: 6px;
                padding: 8px 12px;
            }
            QPushButton:hover {
                background-color: #505050;
            }
            QPushButton:pressed {
                background-color: #2f5d9b;
            }
            QTimeEdit, QListWidget {
                background-color: #2b2b2b;
                border: 1px solid #555;
                border-radius: 6px;
                padding: 6px;
            }
            QListWidget::item {
                padding: 6px;
            }
            QListWidget::item:selected {
                background-color: #2f5d9b;
            }
            """
        )

    def _start_timers(self) -> None:
        self.clock_timer = QtCore.QTimer(self)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)

        self.alarm_timer = QtCore.QTimer(self)
        self.alarm_timer.timeout.connect(self._check_alarm)
        self.alarm_timer.start(1000)

        self.refresh_timer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self.refresh_timer.start(10 * 60 * 1000)  # 10 minutes

    def _update_clock(self) -> None:
        now_utc = datetime.now(timezone.utc)
        self.clock_label.setText(now_utc.strftime("%H:%M:%S"))

    def _check_alarm(self) -> None:
        now_local = datetime.now()
        self.alarm.check_alarm(now_local)

    def _set_alarm(self) -> None:
        alarm_time = self.alarm_time_edit.time().toPyTime()
        self.alarm.set_alarm(alarm_time)
        self.statusBar().showMessage(f"Wecker gesetzt auf {alarm_time.strftime('%H:%M')}")

    def _clear_alarm(self) -> None:
        self.alarm.clear_alarm()
        self.statusBar().showMessage("Wecker deaktiviert")

    def _on_alarm_triggered(self) -> None:
        self.statusBar().showMessage("Wecker aktiv! Piezo eingeschaltet.")

    def _apply_favorite(self, index: int) -> None:
        favorite_time = self.favorites.get_favorite(index)
        if favorite_time is None:
            self.statusBar().showMessage(f"Favorit {index + 1} ist noch nicht gesetzt")
            return
        self.alarm_time_edit.setTime(favorite_time)
        self.statusBar().showMessage(
            f"Favorit {index + 1} geladen: {favorite_time.toString('HH:mm')}"
        )

    def _save_favorite(self, index: int) -> None:
        selected_time = self.alarm_time_edit.time()
        self.favorites.set_favorite(index, selected_time)
        self._load_favorites()
        self.statusBar().showMessage(
            f"Favorit {index + 1} gespeichert: {selected_time.toString('HH:mm')}"
        )

    def refresh_data(self) -> None:
        self._load_todoist()
        self._load_calendar()

    def _load_todoist(self) -> None:
        self.todo_list.clear()
        try:
            items = self.todo_client.fetch_today()
        except requests.RequestException as exc:  # pragma: no cover - runtime only
            self.todo_list.addItem(f"Todoist Fehler: {exc}")
            return

        for item in items:
            text = item.content
            if item.due:
                text = f"{text} (fällig {item.due})"
            self.todo_list.addItem(text)

    def _load_favorites(self) -> None:
        for idx, button in enumerate(self.favorite_buttons):
            favorite_time = self.favorites.get_favorite(idx)
            if favorite_time:
                button.setText(f"Favorit {idx + 1}: {favorite_time.toString('HH:mm')}")
            else:
                button.setText(f"Favorit {idx + 1}: leer")

    def _load_calendar(self) -> None:
        self.calendar_list.clear()
        try:
            events = self.calendar_client.fetch_today()
        except Exception as exc:  # pragma: no cover - runtime only
            self.calendar_list.addItem(f"Kalender Fehler: {exc}")
            return

        for event in events:
            start_local = event.start.astimezone()
            end_local = event.end.astimezone()
            text = f"{start_local:%H:%M} - {end_local:%H:%M}: {event.summary}"
            self.calendar_list.addItem(text)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # pragma: no cover
        self.alarm.clear_alarm()
        GPIO.cleanup()
        event.accept()


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    window = DashboardWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
