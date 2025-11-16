import sys
import random
import os
import time
from PyQt5 import QtWidgets, QtCore
import RPi.GPIO as GPIO

class AlarmClock(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.alarm_time = None
        self.alarm_active = False
        self.snooze_active = False

    def initUI(self):
        self.setWindowTitle('Alarm Clock')
        self.setGeometry(100, 100, 400, 300)

        self.time_label = QtWidgets.QLabel('Set Alarm Time (HH:MM):', self)
        self.time_label.move(20, 20)

        self.time_input = QtWidgets.QTimeEdit(self)
        self.time_input.move(20, 50)

        self.set_alarm_btn = QtWidgets.QPushButton('Set Alarm', self)
        self.set_alarm_btn.move(20, 100)
        self.set_alarm_btn.clicked.connect(self.set_alarm)

        self.dismiss_btn = QtWidgets.QPushButton('Dismiss Alarm', self)
        self.dismiss_btn.move(150, 100)
        self.dismiss_btn.clicked.connect(self.dismiss_alarm)
        self.dismiss_btn.setDisabled(True)

        self.snooze_btn = QtWidgets.QPushButton('Snooze', self)
        self.snooze_btn.move(250, 100)
        self.snooze_btn.clicked.connect(self.snooze_alarm)
        self.snooze_btn.setDisabled(True)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.check_alarm)
        self.timer.start(1000)

    def set_alarm(self):
        self.alarm_time = self.time_input.time()
        self.alarm_active = True
        self.dismiss_btn.setEnabled(True)
        self.snooze_btn.setDisabled(False)

    def check_alarm(self):
        if self.alarm_active:
            current_time = QtCore.QTime.currentTime()
            if self.alarm_time.hour() == current_time.hour() and self.alarm_time.minute() == current_time.minute():
                self.trigger_alarm()
                self.alarm_active = False  # Turn off alarm after triggering

    def trigger_alarm(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(18, GPIO.OUT)
        GPIO.output(18, GPIO.HIGH)  # Sound Alarm
        self.dismiss_btn.setDisabled(False)
        self.snooze_btn.setDisabled(False)
        self.play_random_video()

    def dismiss_alarm(self):
        GPIO.output(18, GPIO.LOW)  # Stop Alarm
        self.dismiss_btn.setDisabled(True)
        self.snooze_btn.setDisabled(False)
        self.snooze_active = False

    def snooze_alarm(self):
        self.snooze_active = True
        self.dismiss_btn.setDisabled(True)
        time.sleep(30)  # 30 seconds snooze
        self.trigger_alarm()  # Re-trigger alarm

    def play_random_video(self):
        video_folder = '/videos'
        videos = [f for f in os.listdir(video_folder) if f.endswith('.mp4')]
        if videos:
            selected_video = random.choice(videos)
            os.system(f'omxplayer {video_folder}/{selected_video}')  # Play random video

if __name__ == '__main__':
    import sys
    GPIO.setwarnings(False)  # Ignore warnings for GPIO
    app = QtWidgets.QApplication(sys.argv)
    clock = AlarmClock()
    clock.show()
    sys.exit(app.exec_())