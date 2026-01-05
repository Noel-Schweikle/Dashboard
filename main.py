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
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
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
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return datetime.fromisoformat(value + "T00:00:00+00:00")


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
        alarm_layout = QtWidgets.QHBoxLayout()
        self.alarm_time_edit = QtWidgets.QTimeEdit(QtCore.QTime.currentTime())
        self.alarm_time_edit.setDisplayFormat("HH:mm")
        self.alarm_time_edit.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        self.alarm_button = QtWidgets.QPushButton("Wecker setzen")
        self.alarm_button.clicked.connect(self._set_alarm)
        self.clear_alarm_button = QtWidgets.QPushButton("Wecker stoppen")
        self.clear_alarm_button.clicked.connect(self._clear_alarm)
        alarm_layout.addWidget(self.alarm_time_edit)
        alarm_layout.addWidget(self.alarm_button)
        alarm_layout.addWidget(self.clear_alarm_button)
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
        self._start_timers()
        self.refresh_data()

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
