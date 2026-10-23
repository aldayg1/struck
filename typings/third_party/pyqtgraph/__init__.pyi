"""Partial interface stubs for pyqtgraph (untyped upstream package).

Covers the pyqtgraph API surface Struck's charts use:
``PlotWidget`` (a ``QGraphicsView`` wrapping a ``PlotItem``), the
``PlotDataItem`` objects its ``plot()`` returns, ``AxisItem`` handles from
``getAxis()``, and the ``mkPen`` QPen factory. Signatures mirror the
installed pyqtgraph sources (``widgets/PlotWidget.py``,
``graphicsItems/PlotItem/PlotItem.py``, ``graphicsItems/ViewBox/ViewBox.py``,
``functions.py``); permissive parameters are typed ``object`` because
pyqtgraph genuinely accepts many shapes there.
"""

from PySide6 import QtCore, QtGui
from PySide6.QtWidgets import QGraphicsView, QWidget

class AxisItem:
    def setStyle(self, **kwds: object) -> None: ...
    def setWidth(self, w: int | None = ...) -> None: ...  # Struck addition
    def setTextPen(self, *args: object, **kargs: object) -> None: ...
    def setTickFont(self, font: QtGui.QFont) -> None: ...  # Struck addition
    def setPen(self, *args: object, **kargs: object) -> None: ...  # Struck addition
    def setTicks(
        self, ticks: list[list[tuple[float, str]]] | None
    ) -> None: ...  # Struck addition

class PlotDataItem:
    def setPen(self, *args: object, **kargs: object) -> None: ...
    def setVisible(self, visible: bool) -> None: ...
    def setData(self, *args: object, **kargs: object) -> None: ...

class ViewBox(QtCore.QObject):
    XAxis: int
    YAxis: int
    sigRangeChangedManually: QtCore.Signal
    sigRangeChanged: QtCore.Signal
    sigXRangeChanged: QtCore.Signal  # Struck addition
    def setMouseEnabled(self, x: bool | None = ..., y: bool | None = ...) -> None: ...
    def setMenuEnabled(self, enableMenu: bool = ...) -> None: ...
    def invertY(self, b: bool = ...) -> None: ...
    def invertX(self, b: bool = ..., allowInvert: bool = ...) -> None: ...
    def setAutoVisible(self, x: bool | None = ..., y: bool | None = ...) -> None: ...
    def enableAutoRange(
        self,
        axis: object | None = ...,
        enable: bool = ...,
        x: bool | None = ...,
        y: bool | None = ...,
    ) -> None: ...
    def disableAutoRange(self, axis: object | None = ...) -> None: ...
    def autoRange(self, padding: object | None = ...) -> None: ...
    def setLimits(self, **kwds: object) -> None: ...
    def mapSceneToView(self, obj: QtCore.QPointF) -> QtCore.QPointF: ...  # Struck addition
    def viewRange(self) -> list[list[float]]: ...  # Struck addition
    def rect(self) -> QtCore.QRectF: ...  # Struck addition
    def mapFromView(self, obj: QtCore.QPointF) -> QtCore.QPointF: ...  # Struck addition
    def mapDeviceToView(self, obj: QtCore.QPointF) -> QtCore.QPointF: ...  # Struck addition

class PlotItem(QtCore.QObject):
    def __init__(
        self,
        parent: QWidget | None = ...,
        name: str | None = ...,
        labels: object | None = ...,
        title: str | None = ...,
        viewBox: ViewBox | None = ...,
        axisItems: dict[str, AxisItem] | None = ...,
        enableMenu: bool = ...,
        **kargs: object,
    ) -> None: ...
    def setMouseEnabled(self, x: bool | None = ..., y: bool | None = ...) -> None: ...
    def setMenuEnabled(self, enableMenu: bool = ...) -> None: ...
    def getViewBox(self) -> ViewBox: ...

class PlotWidget(QGraphicsView):
    def __init__(
        self,
        parent: QWidget | None = ...,
        background: object = ...,
        plotItem: object | None = ...,
        **kargs: object,
    ) -> None: ...
    def plot(
        self,
        *args: object,
        pen: QtGui.QPen | None = ...,
        name: str | None = ...,
        **kargs: object,
    ) -> PlotDataItem: ...
    def clear(self) -> None: ...
    def removeItem(self, item: object) -> None: ...
    def addItem(self, item: object, *args: object, **kargs: object) -> None: ...
    def enableAutoRange(
        self,
        axis: object | None = ...,
        enable: bool = ...,
        x: bool | None = ...,
        y: bool | None = ...,
    ) -> None: ...
    def disableAutoRange(self, axis: object | None = ...) -> None: ...
    def setBackground(self, *args: object, **kargs: object) -> None: ...
    def showGrid(
        self, x: bool | None = ..., y: bool | None = ..., alpha: float | None = ...
    ) -> None: ...
    def setLabel(self, axis: str, *args: object, **kargs: object) -> None: ...
    def getAxis(self, axis: str) -> AxisItem: ...
    def getPlotItem(self) -> PlotItem: ...
    def getViewBox(self) -> ViewBox: ...
    def hideAxis(self, axis: str) -> None: ...  # Struck addition
    def setXLink(self, view: object) -> None: ...  # Struck addition
    def setYRange(
        self, min: float, max: float, padding: float | None = ...
    ) -> None: ...  # Struck addition
    def setXRange(
        self, min: float, max: float, padding: float | None = ...
    ) -> None: ...  # Struck addition

def mkPen(
    *args: str | QtGui.QColor | QtGui.QPen | None,
    color: str | QtGui.QColor | QtGui.QPen | None = ...,
    width: int = ...,
    style: QtCore.Qt.PenStyle = ...,
    dash: list[int] | None = ...,
    cosmetic: bool = ...,
) -> QtGui.QPen: ...

# ── Struck additions (2026-10-21): stock items the Struck chart composes.
# Signatures mirror pyqtgraph 0.14.0 graphicsItems/{BarGraphItem,
# InfiniteLine,DateAxisItem,TextItem}.py and functions.py.

class GraphicsObject(QtCore.QObject): ...

class BarGraphItem(GraphicsObject):
    def __init__(self, **opts: object) -> None: ...
    def setOpts(self, **opts: object) -> None: ...

class InfiniteLine(GraphicsObject):
    def __init__(
        self,
        pos: float | None = ...,
        angle: float = ...,
        pen: QtGui.QPen | None = ...,
        movable: bool = ...,
        label: str | None = ...,
        labelOpts: dict[str, object] | None = ...,
        name: str | None = ...,
    ) -> None: ...
    def setPos(self, pos: float) -> None: ...
    def value(self) -> float: ...  # Struck addition

class DateAxisItem(AxisItem):
    def __init__(
        self, orientation: str = ..., utcOffset: float | None = ..., **kwargs: object
    ) -> None: ...

def mkBrush(*args: str | QtGui.QColor) -> QtGui.QBrush: ...
def setConfigOptions(**opts: object) -> None: ...
