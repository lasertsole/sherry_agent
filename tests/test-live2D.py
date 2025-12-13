import pygame
import OpenGL.GL as gl
from pathlib import Path
import live2d.v3 as live2d
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimerEvent, Qt
from live2d.utils.lipsync import WavHandler
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtGui import QMouseEvent, QCursor, QGuiApplication


def callback():
    print("motion end")


class Win(QOpenGLWidget):

    def __init__(self) -> None:
        super().__init__()
        self.isInLA = False
        self.clickInLA = False
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.a = 0
        self.resize(400, 500)
        self.read = False
        self.clickX = -1
        self.clickY = -1
        self.model: live2d.LAppModel | None = None
        self.systemScale = QGuiApplication.primaryScreen().devicePixelRatio()
        self.wav_handler = WavHandler()

        # voice_path = Path("./fixed_voice.wav").as_posix()
        #
        # pygame.init()
        #
        # pygame.mixer.music.load(voice_path)
        # pygame.mixer.music.play()
        #
        # self.wav_handler.Start(voice_path)

    def initializeGL(self) -> None:
        # 将当前窗口作为 OpenGL 的上下文
        # 图形会被绘制到当前窗口
        live2d.glInit()

        # 创建模型
        self.model = live2d.LAppModel()
        current_dir = Path(__file__).parent.resolve()
        model_path = current_dir /  "Sherry - Model Mandou/Sherry - Model.model3.json"
        model_path = model_path.resolve().as_posix()
        self.model.LoadModelJson(model_path)
        # 以 fps = 120 的频率进行绘图
        self.startTimer(int(1000 / 120))

    def resizeGL(self, w: int, h: int) -> None:
        # 使模型的参数按窗口大小进行更新
        if self.model:
            self.model.Resize(w, h)

    def paintGL(self) -> None:
        live2d.clearBuffer()

        # if self.wav_handler.Update():
        #     power = self.wav_handler.GetRms()
        #
        #     self.model.SetParameterValue("ParamMouthOpenY", power * 3.0, 1.0)

        self.model.Update()

        self.model.Draw()

    def timerEvent(self, a0: QTimerEvent | None) -> None:
        if not self.isVisible():
            return

        if self.a == 0:  # 测试一次播放动作和回调函数
            self.model.StartMotion("TapBody", 0, live2d.MotionPriority.FORCE, onFinishMotionHandler=callback)
            self.a += 1

        local_x, local_y = QCursor.pos().x() - self.x(), QCursor.pos().y() - self.y()
        if self.isInL2DArea(local_x, local_y):
            self.isInLA = True
            print("in l2d area")
        else:
            self.isInLA = False

        self.update()

    def isInL2DArea(self, click_x, click_y):
        h = self.height()
        alpha = gl.glReadPixels(click_x * self.systemScale, (h - click_y) * self.systemScale, 1, 1, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)[3]
        return alpha > 0

    def mousePressEvent(self, event: QMouseEvent) -> None:
        x, y = event.scenePosition().x(), event.scenePosition().y()
        # 传入鼠标点击位置的窗口坐标
        if self.isInL2DArea(x, y):
            self.clickInLA = True
            self.clickX, self.clickY = x, y

        self.model.Drag(x, y)

        print("pressed")

    def mouseReleaseEvent(self, event):
        x, y = event.scenePosition().x(), event.scenePosition().y()
        # if self.isInL2DArea(x, y):
        if self.isInLA:
            # self.model.Touch(x, y)
            pass
            self.clickInLA = False
            print("released")

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        x, y = event.scenePosition().x(), event.scenePosition().y()
        if self.clickInLA:
            self.move(int(self.x() + x - self.clickX), int(self.y() + y - self.clickY))



if __name__ == "__main__":
    import sys

    live2d.init()

    app = QApplication(sys.argv)
    win = Win()
    win.show()
    app.exec()

    live2d.dispose()