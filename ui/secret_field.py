from PyQt6.QtCore import QEvent
from qfluentwidgets import PasswordLineEdit, FluentIcon as FIF


class SecretLineEdit(PasswordLineEdit):
    """Password field whose eye button toggles: click to show, click again to hide.

    qfluentwidgets' PasswordLineEdit only shows the text while the button is held
    down, which is awkward for checking a long webhook URL.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewButton.setToolTip("Show")

    def setPasswordVisible(self, isVisible: bool):
        super().setPasswordVisible(isVisible)
        self.viewButton.setIcon(FIF.HIDE if isVisible else FIF.VIEW)
        self.viewButton.setToolTip("Hide" if isVisible else "Show")

    def eventFilter(self, obj, e):
        if obj is self.viewButton and self.isEnabled():
            if e.type() == QEvent.Type.MouseButtonRelease and self.viewButton.rect().contains(e.position().toPoint()):
                self.setPasswordVisible(not self.isPasswordVisible())
            if e.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
                # Skip the parent's hold-to-show handling, keep the button's own
                # pressed/hover painting (LineEditButton's handlers still run).
                return False
        return super().eventFilter(obj, e)
