from __future__ import annotations

import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QRunnable, QThread, QThreadPool, QTimer, Qt, Signal, QObject, QSize
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QHeaderView,
    QSpinBox,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QSystemTrayIcon,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.config import ConfigStore, format_local_datetime, normalize_utc_offset
from trakt_tracker.domain import HistoryItemInput, RatingInput, TitleSummary
from trakt_tracker.infrastructure.cache import BinaryCache
from trakt_tracker.startup_profile import StartupProfiler

_DESKTOP_UI_SCALE = 1.5


def _scale_px(value: int) -> int:
    return max(1, int(round(value * _DESKTOP_UI_SCALE)))


def _format_compact_votes(value: int | None) -> str:
    if value is None:
        return ""
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.2f}".rstrip("0").rstrip(".") + "k"
    return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"


def _format_rating_with_votes(rating: float | None, votes: int | None) -> str:
    if rating is None:
        return "n/a"
    compact_votes = _format_compact_votes(votes)
    if compact_votes:
        return f"{rating:.1f} ({compact_votes})"
    return f"{rating:.1f}"


def _format_app_datetime(value: datetime | None, utc_offset: str) -> str:
    return format_local_datetime(value, utc_offset) if value is not None else "unknown"


def _is_recent_progress_release(progress, *, hours: int = 48) -> bool:
    next_episode = getattr(progress, "next_episode", None)
    if next_episode is None or next_episode.first_aired is None:
        return False
    release_at = next_episode.first_aired
    if release_at.tzinfo is None:
        release_at = release_at.replace(tzinfo=UTC)
    now = datetime.now(tz=UTC)
    return release_at <= now <= (release_at + timedelta(hours=hours))


def _has_released_next_episode(progress) -> bool:
    next_episode = getattr(progress, "next_episode", None)
    if next_episode is None or next_episode.first_aired is None:
        return False
    release_at = next_episode.first_aired
    if release_at.tzinfo is None:
        release_at = release_at.replace(tzinfo=UTC)
    return release_at <= datetime.now(tz=UTC)


def _effective_progress_aired(progress) -> int:
    aired = int(getattr(progress, "aired", 0) or 0)
    completed = int(getattr(progress, "completed", 0) or 0)
    if _has_released_next_episode(progress):
        return max(aired, completed + 1)
    return aired


def _effective_progress_percent(progress) -> float:
    aired = _effective_progress_aired(progress)
    if aired <= 0:
        return 0.0
    completed = float(getattr(progress, "completed", 0) or 0)
    return (completed / aired) * 100.0


def _build_drop_icon(
    size: int = 16,
    *,
    stroke_color: str = "#3b3b3b",
) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(stroke_color))
    pen.setWidth(2)
    painter.setPen(pen)
    inset = 2
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(inset, inset, size - inset * 2, size - inset * 2)
    mid_y = size // 2
    painter.drawLine(inset + 3, mid_y, size - inset - 3, mid_y)
    painter.end()
    return QIcon(pixmap)


def _ui_asset_path(name: str) -> str:
    return str(Path(__file__).with_name("assets") / name)


def load_app_icon() -> QIcon:
    return QIcon(_ui_asset_path("trakt_logo_bw.svg"))


class OnboardingDialog(QDialog):
    def __init__(self, services: ServiceContainer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.setWindowTitle("Trakt Setup")
        self.setModal(True)

        config = self.services.auth.config
        self.client_id = QLineEdit(config.client_id)
        self.client_secret = QLineEdit(config.client_secret)
        self.client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.redirect_uri = QLineEdit(config.redirect_uri)
        self.tmdb_token = QLineEdit(config.tmdb_read_access_token)
        self.tmdb_api_key = QLineEdit(config.tmdb_api_key)
        self.kinopoisk_api_key = QLineEdit(config.kinopoisk_api_key)
        self.embedded_player_checkbox = QCheckBox("Open Kinopoisk in embedded player")

        form = QFormLayout()
        form.addRow("Client ID", self.client_id)
        form.addRow("Client Secret", self.client_secret)
        form.addRow("Redirect URI", self.redirect_uri)
        form.addRow("TMDb Read Token", self.tmdb_token)
        form.addRow("TMDb API Key", self.tmdb_api_key)
        form.addRow("Kinopoisk API Key", self.kinopoisk_api_key)
        form.addRow("", self.embedded_player_checkbox)

        info = QLabel(
            "Create a Trakt API application, set redirect URI "
            "`http://127.0.0.1:8765/callback`, then save settings and authorize."
        )
        info.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def accept(self) -> None:
        self.services.auth.update_config(
            self.client_id.text(),
            self.client_secret.text(),
            self.redirect_uri.text(),
            self.tmdb_api_key.text(),
            self.tmdb_token.text(),
            self.kinopoisk_api_key.text(),
        )
        self.services.auth.config.open_in_embedded_player = self.embedded_player_checkbox.isChecked()
        ConfigStore().save(self.services.auth.config)
        super().accept()


class RatingDialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Rate: {title}")
        self.skipped = False
        self.rating = QSpinBox()
        self.rating.setRange(1, 10)
        self.rating.setValue(8)

        form = QFormLayout()
        form.addRow("Rating", self.rating)

        buttons = QDialogButtonBox()
        ok_button = buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Save rating")
        skip_button = buttons.addButton("Skip", QDialogButtonBox.ButtonRole.ActionRole)
        cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        skip_button.clicked.connect(self._skip)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _skip(self) -> None:
        self.skipped = True
        self.done(QDialog.DialogCode.Accepted)


class PlayerWindow(QMainWindow):
    def __init__(self, title: str, target_url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(f"Play: {title}")
        self.resize(1280, 800)
        self._view = QWebEngineView(self)
        self.setCentralWidget(self._view)
        self._view.setUrl(QUrl(target_url))


class PlayWatchPromptCard(QWidget):
    def __init__(
        self,
        *,
        trakt_id: int,
        title: str,
        episode_label: str,
        on_watch: Callable[[int], None],
        on_dismiss: Callable[[int], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._trakt_id = trakt_id
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("playWatchPrompt")

        title_label = QLabel(title)
        title_label.setObjectName("playWatchPromptTitle")
        title_label.setWordWrap(True)

        body_label = QLabel(f"Finish watching? {episode_label}")
        body_label.setObjectName("playWatchPromptBody")
        body_label.setWordWrap(True)

        watch_btn = QPushButton("Watched")
        watch_btn.clicked.connect(lambda: on_watch(self._trakt_id))
        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.clicked.connect(lambda: on_dismiss(self._trakt_id))

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(_scale_px(8))
        actions.addWidget(watch_btn)
        actions.addWidget(dismiss_btn)
        actions.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_scale_px(16), _scale_px(16), _scale_px(16), _scale_px(16))
        layout.setSpacing(_scale_px(10))
        layout.addWidget(title_label)
        layout.addWidget(body_label)
        layout.addLayout(actions)

        self.setStyleSheet(
            """
            QWidget#playWatchPrompt {
                background: rgba(255, 255, 255, 0.98);
                border: 1px solid #d7d2c7;
                border-radius: 18px;
            }
            QLabel#playWatchPromptTitle {
                color: #1d1d1d;
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#playWatchPromptBody {
                color: #625a4c;
                font-size: 14px;
            }
            """
        )


class DebugToastCard(QWidget):
    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setStyleSheet("color: #1d1d1d; font-size: 14px;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(label)
        self.setStyleSheet(
            """
            QWidget {
                background: rgba(255, 255, 255, 0.98);
                border: 1px solid #d9ddd3;
                border-radius: 14px;
            }
            """
        )


class HistoryDialog(QDialog):
    def __init__(self, title: TitleSummary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Add to history: {title.title}")
        self.title = title
        self.watched_at = QDateTimeEdit(datetime.now())
        self.watched_at.setCalendarPopup(True)
        self.watched_at.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.season = QSpinBox()
        self.season.setRange(0, 999)
        self.episode = QSpinBox()
        self.episode.setRange(0, 999)
        if title.title_type == "movie":
            self.season.setEnabled(False)
            self.episode.setEnabled(False)

        form = QFormLayout()
        form.addRow("Watched at", self.watched_at)
        form.addRow("Season", self.season)
        form.addRow("Episode", self.episode)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def to_input(self) -> HistoryItemInput:
        season = self.season.value() if self.title.title_type == "show" else None
        episode = self.episode.value() if self.title.title_type == "show" else None
        if season == 0:
            season = None
        if episode == 0:
            episode = None
        return HistoryItemInput(
            title_type=self.title.title_type,
            trakt_id=self.title.trakt_id,
            watched_at=self.watched_at.dateTime().toPython(),
            season=season,
            episode=episode,
            title=self.title.title,
        )


class TitleDetailsDialog(QDialog):
    def __init__(self, services: ServiceContainer, title: TitleSummary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.title_info = title
        self.setWindowTitle(title.title)
        self.resize(560, 480)

        self.summary = QTextEdit()
        self.summary.setReadOnly(True)

        rate_btn = QPushButton("Set rating")
        rate_btn.clicked.connect(self._rate)
        history_btn = QPushButton("Add to history")
        history_btn.clicked.connect(self._history)
        refresh_btn = QPushButton("Refresh progress")
        refresh_btn.clicked.connect(self._refresh_progress)
        if title.title_type != "show":
            refresh_btn.setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(rate_btn)
        row.addWidget(history_btn)
        row.addWidget(refresh_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(row)
        layout.addWidget(self.summary)
        self._render()

    def _render(self) -> None:
        title = self.services.catalog.get_title_details(self.title_info.trakt_id, self.title_info.title_type)
        text = [
            f"{title.title} ({title.year or 'n/a'})",
            f"Type: {title.title_type}",
            f"Status: {title.status or 'n/a'}",
            "",
            title.overview or "No overview.",
        ]
        self.summary.setPlainText("\n".join(text))

    def _rate(self) -> None:
        dialog = RatingDialog(self.title_info.title, self)
        if dialog.exec():
            self.services.library.set_rating(
                RatingInput(
                    title_type=self.title_info.title_type,
                    trakt_id=self.title_info.trakt_id,
                    rating=dialog.rating.value(),
                ),
                title=self.title_info.title,
            )
            QMessageBox.information(self, "Saved", "Rating saved.")

    def _history(self) -> None:
        dialog = HistoryDialog(self.title_info, self)
        if dialog.exec():
            self.services.library.add_history_item(dialog.to_input())
            QMessageBox.information(self, "Saved", "History item saved.")

    def _refresh_progress(self) -> None:
        if self.title_info.title_type != "show":
            return
        progress = self.services.progress.refresh_show_progress(self.title_info.trakt_id)
        lines = [
            self.summary.toPlainText(),
            "",
            f"Progress: {progress.completed}/{progress.aired} ({progress.percent_completed:.1f}%)",
        ]
        if progress.last_episode:
            lines.append(
                f"Last watched: S{progress.last_episode.season:02d}E{progress.last_episode.number:02d} {progress.last_episode.title}"
            )
        if progress.next_episode:
            lines.append(
                f"Next episode: S{progress.next_episode.season:02d}E{progress.next_episode.number:02d} {progress.next_episode.title}"
            )
        self.summary.setPlainText("\n".join(lines))


class SearchResultWidget(QWidget):
    def __init__(self, title: TitleSummary, pixmap: QPixmap | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(160)
        poster = QLabel()
        poster.setFixedSize(96, 144)
        poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        poster.setStyleSheet("border: 1px solid #666; background: #202020; color: #ddd;")
        if pixmap is not None and not pixmap.isNull():
            poster.setPixmap(
                pixmap.scaled(
                    poster.width(),
                    poster.height(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            poster.setText("No\nposter")

        title_label = QLabel(f"{title.title} ({title.year or 'n/a'})")
        title_label.setStyleSheet("font-size: 14px; font-weight: 700; color: #111111;")
        title_label.setWordWrap(True)

        trakt_rating = _format_rating_with_votes(title.trakt_rating, title.trakt_votes)
        tmdb_rating = _format_rating_with_votes(title.tmdb_rating, title.tmdb_votes)
        imdb_rating = _format_rating_with_votes(title.imdb_rating, title.imdb_votes)
        meta_text = f"{'Serial' if title.title_type == 'show' else 'Movie'} | Status: {title.status or 'n/a'}"
        ratings_text = f"Trakt: {trakt_rating} | TMDb: {tmdb_rating} | IMDb: {imdb_rating}"
        if title.imdb_id:
            ratings_text += f" | {title.imdb_id}"

        meta_label = QLabel(meta_text)
        meta_label.setStyleSheet("color: #5a5a5a; font-size: 12px;")
        ratings_label = QLabel(ratings_text)
        ratings_label.setStyleSheet("color: #1d4ed8; font-size: 12px; font-weight: 600;")

        overview_label = QLabel(title.overview or "No overview.")
        overview_label.setWordWrap(True)
        overview_label.setMaximumHeight(72)
        overview_label.setStyleSheet("color: #3a3a3a; font-size: 12px;")

        text_col = QVBoxLayout()
        text_col.addWidget(title_label)
        text_col.addWidget(meta_label)
        text_col.addWidget(ratings_label)
        text_col.addWidget(overview_label)
        text_col.addStretch()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        layout.addWidget(poster)
        layout.addLayout(text_col, 1)


class ProgressPosterWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(189, 284)
        self._pixmap: QPixmap | None = None
        self._failed = False
        self._badge_text = ""
        self._rating_parts: list[tuple[str, str]] = []

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self._failed = pixmap is None or pixmap.isNull()
        self.update()

    def set_loading_state(self, failed: bool) -> None:
        self._pixmap = None
        self._failed = failed
        self.update()

    def set_badge_text(self, text: str) -> None:
        self._badge_text = text.strip()
        self.update()

    def set_rating_parts(self, parts: list[tuple[str, str]]) -> None:
        self._rating_parts = parts
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        painter.fillRect(rect, QColor("#202020"))
        if self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            source_x = max(0, (scaled.width() - rect.width()) // 2)
            source_y = max(0, (scaled.height() - rect.height()) // 2)
            painter.drawPixmap(rect, scaled, QRect(source_x, source_y, rect.width(), rect.height()))
        else:
            painter.setPen(QColor("#dddddd"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No poster" if self._failed else "Loading...")
        if self._badge_text:
            badge_rect = QRect(rect.right() - 44, 8, 36, 36)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#dc2626"))
            painter.drawRoundedRect(badge_rect, 11, 11)
            badge_font = painter.font()
            badge_font.setBold(True)
            badge_font.setPointSizeF(max(12.0, badge_font.pointSizeF() * 1.18))
            painter.setFont(badge_font)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, self._badge_text)
        if self._rating_parts:
            chip_height = 42 if len(self._rating_parts) > 1 else 28
            chip_rect = QRect(12, rect.bottom() - (chip_height + 12), rect.width() - 24, chip_height)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(55, 65, 81, 230))
            painter.drawRoundedRect(chip_rect, 14, 14)
            icon_height = 16
            gap = 6
            font = painter.font()
            font.setBold(True)
            font.setPointSizeF(max(8.5, font.pointSizeF() * 0.78))
            painter.setFont(font)
            metrics = painter.fontMetrics()
            rendered_parts: list[tuple[QPixmap, str]] = []
            for source, text in self._rating_parts:
                icon_name = "imdb_icon.png" if source == "imdb" else "trakt_logo_bw.svg"
                icon = QIcon(_ui_asset_path(icon_name)).pixmap(QSize(icon_height, icon_height))
                rendered_parts.append((icon, text))

            row_height = chip_rect.height() // max(1, len(rendered_parts))
            for index, (icon, text) in enumerate(rendered_parts):
                text_width = metrics.horizontalAdvance(text)
                row_top = chip_rect.top() + index * row_height
                total_width = icon.width() + gap + text_width
                x = chip_rect.left() + max(8, (chip_rect.width() - total_width) // 2)
                y = row_top + max(0, (row_height - icon.height()) // 2)
                painter.drawPixmap(x, y, icon)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(
                    x + icon.width() + gap,
                    row_top + 1,
                    text_width,
                    row_height,
                    Qt.AlignmentFlag.AlignVCenter,
                    text,
                )


class ProgressCard(QWidget):
    def __init__(
        self,
        progress,
        on_play,
        on_mark_watched,
        on_drop_toggle,
        *,
        utc_offset: str,
        is_unseen_release: bool = False,
        on_open_new: Callable[[int], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.progress = progress
        is_recent_release = is_unseen_release and not progress.is_dropped
        self._on_open_new = on_open_new
        self._is_unseen_release = is_recent_release
        self.poster = ProgressPosterWidget()
        skipped_count = max(_effective_progress_aired(progress) - progress.completed, 0)
        self.poster.set_badge_text(str(skipped_count) if skipped_count > 0 else "")
        self.poster.set_rating_parts(self._build_rating_parts(progress))
        self.setMinimumWidth(510)
        self.setMaximumWidth(645)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setObjectName("progressCard")

        title_label = QLabel(progress.title)
        title_label.setObjectName("progressTitle")
        title_label.setWordWrap(True)

        meta_bits = [f"{progress.completed}/{_effective_progress_aired(progress)} watched ({_effective_progress_percent(progress):.1f}%)"]
        if progress.status:
            meta_bits.append(progress.status)
        meta_label = QLabel(" | ".join(meta_bits))
        meta_label.setObjectName("progressMeta")
        meta_label.setWordWrap(True)

        release_label = QLabel("New")
        release_label.setVisible(is_recent_release)
        release_label.setObjectName("progressRelease")

        next_episode = progress.next_episode
        next_lines = []
        if next_episode is not None:
            next_lines.append(f"Next: S{next_episode.season:02d}E{next_episode.number:02d} {next_episode.title}")
            next_lines.append(
                f"Airs: {_format_app_datetime(next_episode.first_aired, utc_offset)}"
                if next_episode.first_aired else "Airs: unknown"
            )
        else:
            next_lines.append("No next episode queued.")
        next_label = QLabel("\n".join(next_lines))
        next_label.setObjectName("progressNext")
        next_label.setWordWrap(True)

        button_size = QSize(48, 42)

        play_btn = QToolButton()
        play_btn.clicked.connect(lambda: on_play(progress.trakt_id))
        play_btn.setToolTip("Play")
        play_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        play_btn.setIconSize(QSize(22, 22))
        play_btn.setFixedSize(button_size)
        play_btn.setAutoRaise(False)
        play_btn.setObjectName("progressAction")

        watch_btn = QToolButton()
        watch_btn.clicked.connect(lambda: on_mark_watched(progress.trakt_id))
        watch_btn.setEnabled(progress.next_episode is not None and not progress.is_dropped)
        watch_btn.setToolTip("Mark watched")
        watch_btn.setIcon(QIcon(_ui_asset_path("watched_check.svg")))
        watch_btn.setIconSize(QSize(22, 22))
        watch_btn.setFixedSize(button_size)
        watch_btn.setAutoRaise(False)
        watch_btn.setObjectName("progressAction")

        drop_btn = QToolButton()
        drop_btn.clicked.connect(lambda: on_drop_toggle(progress.trakt_id))
        drop_btn.setToolTip("Undrop" if progress.is_dropped else "Drop")
        if progress.is_dropped:
            drop_btn.setIcon(_build_drop_icon(stroke_color="#1f7a4d"))
        else:
            drop_btn.setIcon(_build_drop_icon(stroke_color="#b2435a"))
        drop_btn.setIconSize(QSize(22, 22))
        drop_btn.setFixedSize(button_size)
        drop_btn.setAutoRaise(False)
        drop_btn.setObjectName("progressAction")

        button_row = QHBoxLayout()
        button_row.setSpacing(12)
        button_row.addWidget(play_btn)
        button_row.addWidget(watch_btn)
        button_row.addWidget(drop_btn)
        button_row.addStretch()

        text_col = QVBoxLayout()
        text_col.addWidget(title_label)
        text_col.addWidget(release_label, 0, Qt.AlignmentFlag.AlignLeft)
        text_col.addWidget(meta_label)
        text_col.addWidget(next_label)
        text_col.addStretch()
        text_col.addLayout(button_row)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(18)
        layout.addWidget(self.poster)
        layout.addLayout(text_col, 1)
        border_color = "#2f9f73" if is_recent_release else "#d9ddd3"
        background = "#eefaf3" if is_recent_release else "#ffffff"
        self.setStyleSheet(
            f"""
            QWidget#progressCard {{
                background: {background};
                border: 1px solid {border_color};
                border-radius: 18px;
            }}
            QLabel#progressTitle {{
                color: #1d1d1d;
                font-size: 24px;
                font-weight: 700;
                background: transparent;
                border: none;
                padding: 0;
            }}
            QLabel#progressRelease {{
                background: #dff6e7;
                color: #0f6a4d;
                border: 1px solid #7cc8a1;
                border-radius: 14px;
                padding: 5px 12px;
                font-size: 15px;
                font-weight: 700;
            }}
            QLabel#progressMeta, QLabel#progressNext {{
                background: transparent;
                border: none;
                padding: 0;
            }}
            QLabel#progressMeta {{
                color: #6a6256;
                font-size: 18px;
            }}
            QLabel#progressNext {{
                color: #1d4ed8;
                font-size: 18px;
                font-weight: 600;
            }}
            QToolButton#progressAction {{
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid #ddd4c6;
                border-radius: 14px;
                padding: 0;
            }}
            QToolButton#progressAction:hover {{
                background: #ffffff;
                border: 1px solid #cdbfa9;
            }}
            QToolButton#progressAction:disabled {{
                color: #9e988f;
                background: rgba(255, 255, 255, 0.6);
                border: 1px solid #e6dfd3;
            }}
            """
        )
        if is_recent_release:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self.poster.set_pixmap(pixmap)

    def set_loading_state(self, failed: bool) -> None:
        self.poster.set_loading_state(failed)

    @staticmethod
    def _build_rating_parts(progress) -> list[tuple[str, str]]:
        next_episode = progress.next_episode
        if next_episode is None:
            return []
        parts: list[tuple[str, str]] = []
        trakt_text = _format_rating_with_votes(next_episode.trakt_rating, next_episode.trakt_votes)
        imdb_text = _format_rating_with_votes(next_episode.imdb_rating, next_episode.imdb_votes)
        if trakt_text != "n/a":
            parts.append(("trakt", trakt_text))
        if imdb_text != "n/a":
            parts.append(("imdb", imdb_text))
        return parts

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if (
            self._is_unseen_release
            and self._on_open_new is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._on_open_new(self.progress.trakt_id)
            event.accept()
            return
        super().mousePressEvent(event)


class SortableTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other) -> bool:
        if not isinstance(other, QTableWidgetItem):
            return super().__lt__(other)
        self_key = self.data(Qt.ItemDataRole.UserRole)
        other_key = other.data(Qt.ItemDataRole.UserRole)
        if self_key is not None and other_key is not None:
            return self_key < other_key
        return super().__lt__(other)


class SearchResultsModel(QAbstractListModel):
    TitleRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self) -> None:
        super().__init__()
        self._all_results: list[TitleSummary] = []
        self._loaded_count = 0
        self._batch_size = 10

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return self._loaded_count

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < self._loaded_count):
            return None
        title = self._all_results[index.row()]
        if role == self.TitleRole:
            return title
        if role == Qt.ItemDataRole.DisplayRole:
            return title.title
        return None

    def set_results(self, results: list[TitleSummary]) -> None:
        self.beginResetModel()
        self._all_results = list(results)
        self._loaded_count = min(self._batch_size, len(self._all_results))
        self.endResetModel()

    def replace_results(self, results: list[TitleSummary], preserve_loaded_count: bool = True) -> None:
        loaded_count = self._loaded_count if preserve_loaded_count else self._batch_size
        self.beginResetModel()
        self._all_results = list(results)
        self._loaded_count = min(max(loaded_count, self._batch_size), len(self._all_results)) if self._all_results else 0
        self.endResetModel()

    def append_results(self, results: list[TitleSummary]) -> None:
        if not results:
            return
        start_all = len(self._all_results)
        self._all_results.extend(results)
        start_visible = self._loaded_count
        add_visible = min(len(results), self._batch_size if self._loaded_count == 0 else len(results))
        if add_visible <= 0:
            return
        self.beginInsertRows(QModelIndex(), start_visible, start_visible + add_visible - 1)
        self._loaded_count += add_visible
        self.endInsertRows()

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:
        if parent.isValid():
            return False
        return self._loaded_count < len(self._all_results)

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:
        if parent.isValid():
            return
        remaining = len(self._all_results) - self._loaded_count
        if remaining <= 0:
            return
        amount = min(self._batch_size, remaining)
        start = self._loaded_count
        end = start + amount - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self._loaded_count += amount
        self.endInsertRows()

    def loaded_count(self) -> int:
        return self._loaded_count

    def total_count(self) -> int:
        return len(self._all_results)

    def title_at(self, row: int) -> TitleSummary | None:
        if 0 <= row < self._loaded_count:
            return self._all_results[row]
        return None

    def update_title(self, row: int, title: TitleSummary) -> None:
        if not (0 <= row < len(self._all_results)):
            return
        self._all_results[row] = title
        if row < self._loaded_count:
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, [self.TitleRole, Qt.ItemDataRole.DisplayRole])

    def rows_for_poster(self, poster_url: str) -> list[int]:
        rows: list[int] = []
        if not poster_url:
            return rows
        for idx in range(self._loaded_count):
            if self._all_results[idx].poster_url == poster_url:
                rows.append(idx)
        return rows


class PosterSignalBridge(QObject):
    loaded = Signal(str)
    failed = Signal(str)


class PosterLoadTask(QRunnable):
    def __init__(self, poster_url: str, disk_cache: BinaryCache, ttl_hours: int, memory_cache: dict[str, QPixmap], bridge: PosterSignalBridge) -> None:
        super().__init__()
        self.poster_url = poster_url
        self.disk_cache = disk_cache
        self.ttl_hours = ttl_hours
        self.memory_cache = memory_cache
        self.bridge = bridge

    def run(self) -> None:
        if not self.poster_url:
            return
        cached_bytes = self.disk_cache.get_bytes(self.poster_url, self.ttl_hours)
        if cached_bytes is not None:
            pixmap = QPixmap()
            if pixmap.loadFromData(cached_bytes):
                self.memory_cache[self.poster_url] = pixmap
                self.bridge.loaded.emit(self.poster_url)
                return
            self.bridge.failed.emit(self.poster_url)
            return
        try:
            request = Request(
                self.poster_url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                },
            )
            with urlopen(request, timeout=20) as response:
                data = response.read()
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self.memory_cache[self.poster_url] = pixmap
                self.disk_cache.set_bytes(self.poster_url, data, suffix=".img")
                self.bridge.loaded.emit(self.poster_url)
                return
            self.bridge.failed.emit(self.poster_url)
        except Exception:
            self.bridge.failed.emit(self.poster_url)
            return


class PosterStore:
    def __init__(self, disk_cache: BinaryCache, ttl_getter) -> None:
        self._disk_cache = disk_cache
        self._ttl_getter = ttl_getter
        self._memory_cache: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._pool = QThreadPool.globalInstance()
        self._bridge = PosterSignalBridge()
        self._bridge.loaded.connect(self._on_loaded)
        self._bridge.failed.connect(self._on_failed)

    @property
    def bridge(self) -> PosterSignalBridge:
        return self._bridge

    def get(self, poster_url: str) -> QPixmap | None:
        return self._memory_cache.get(poster_url)

    def is_failed(self, poster_url: str) -> bool:
        return poster_url in self._failed

    def request(self, poster_url: str) -> None:
        if not poster_url or poster_url in self._memory_cache or poster_url in self._pending or poster_url in self._failed:
            return
        self._pending.add(poster_url)
        self._pool.start(
            PosterLoadTask(
                poster_url,
                self._disk_cache,
                max(1, int(self._ttl_getter())),
                self._memory_cache,
                self._bridge,
            )
        )

    def _on_loaded(self, poster_url: str) -> None:
        self._pending.discard(poster_url)
        self._failed.discard(poster_url)

    def _on_failed(self, poster_url: str) -> None:
        self._pending.discard(poster_url)
        self._failed.add(poster_url)


class SearchItemDelegate(QStyledItemDelegate):
    def __init__(self, poster_store: PosterStore, parent=None) -> None:
        super().__init__(parent)
        self.poster_store = poster_store

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(option.rect.width(), _scale_px(160))

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        title: TitleSummary | None = index.data(SearchResultsModel.TitleRole)
        if title is None:
            return
        painter.save()
        rect = option.rect.adjusted(4, 4, -4, -4)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        bg = QColor("#dbeafe") if is_selected else QColor("#ffffff")
        border = QColor("#93c5fd") if is_selected else QColor("#d8dee6")
        painter.fillRect(rect, bg)
        painter.setPen(QPen(border))
        painter.drawRect(rect)

        poster_rect = QRect(rect.left() + _scale_px(8), rect.top() + _scale_px(8), _scale_px(96), _scale_px(144))
        painter.fillRect(poster_rect, QColor("#202020"))
        painter.setPen(QColor("#666666"))
        painter.drawRect(poster_rect)
        pixmap = self.poster_store.get(title.poster_url)
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(poster_rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x = poster_rect.left() + (poster_rect.width() - scaled.width()) // 2
            y = poster_rect.top() + (poster_rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            if not self.poster_store.is_failed(title.poster_url):
                self.poster_store.request(title.poster_url)
            painter.setPen(QColor("#dddddd"))
            painter.drawText(
                poster_rect,
                Qt.AlignmentFlag.AlignCenter,
                "No poster" if self.poster_store.is_failed(title.poster_url) else "Loading",
            )

        text_left = poster_rect.right() + _scale_px(12)
        text_width = rect.width() - (poster_rect.width() + _scale_px(28))
        line1 = QRect(text_left, rect.top() + _scale_px(10), text_width, _scale_px(28))
        line2 = QRect(text_left, rect.top() + _scale_px(46), text_width, _scale_px(24))
        line3 = QRect(text_left, rect.top() + _scale_px(78), text_width, _scale_px(24))
        line4 = QRect(text_left, rect.top() + _scale_px(110), text_width, _scale_px(78))

        title_font = painter.font()
        title_font.setPointSizeF(max(10.0, title_font.pointSizeF() * 1.2))
        title_font.setBold(True)
        meta_font = painter.font()
        meta_font.setPointSizeF(max(9.0, meta_font.pointSizeF() * 1.08))
        ratings_font = painter.font()
        ratings_font.setPointSizeF(max(9.0, ratings_font.pointSizeF() * 1.08))
        ratings_font.setBold(True)

        painter.setFont(title_font)
        painter.setPen(QColor("#111111"))
        painter.drawText(line1, Qt.TextFlag.TextSingleLine, f"{title.title} ({title.year or 'n/a'})")

        painter.setFont(meta_font)
        painter.setPen(QColor("#5a5a5a"))
        painter.drawText(line2, Qt.TextFlag.TextSingleLine, f"{'Serial' if title.title_type == 'show' else 'Movie'} | Status: {title.status or 'n/a'}")

        trakt_rating = _format_rating_with_votes(title.trakt_rating, title.trakt_votes)
        tmdb_rating = _format_rating_with_votes(title.tmdb_rating, title.tmdb_votes)
        imdb_rating = _format_rating_with_votes(title.imdb_rating, title.imdb_votes)
        painter.setFont(ratings_font)
        painter.setPen(QColor("#1d4ed8"))
        painter.drawText(line3, Qt.TextFlag.TextSingleLine, f"Trakt: {trakt_rating} | TMDb: {tmdb_rating} | IMDb: {imdb_rating}")

        painter.setFont(meta_font)
        painter.setPen(QColor("#3a3a3a"))
        painter.drawText(line4, Qt.TextFlag.TextWordWrap, title.overview or "No overview.")
        painter.restore()


class SearchEnrichmentWorker(QThread):
    item_enriched = Signal(int, object)

    def __init__(self, services: ServiceContainer, results: list[TitleSummary]) -> None:
        super().__init__()
        self.services = services
        self.results = results

    def run(self) -> None:
        for index, title in enumerate(self.results):
            if self.isInterruptionRequested():
                return
            try:
                enriched = self.services.catalog.enrich_title_with_tmdb(title)
            except Exception:
                continue
            if self.isInterruptionRequested():
                return
            self.item_enriched.emit(index, enriched)


class SearchFetchWorker(QThread):
    search_completed = Signal(int, object)
    search_failed = Signal(int, str)

    def __init__(self, services: ServiceContainer, generation: int, query: str, title_type: str | None) -> None:
        super().__init__()
        self.services = services
        self.generation = generation
        self.query = query
        self.title_type = title_type

    def run(self) -> None:
        try:
            results = self.services.catalog.search_titles(self.query, self.title_type)
        except Exception as exc:
            self.search_failed.emit(self.generation, str(exc))
            return
        self.search_completed.emit(self.generation, results)


class IMDbDatasetSyncWorker(QThread):
    sync_completed = Signal(bool)
    sync_failed = Signal(str)
    status_changed = Signal(str)

    def __init__(self, services: ServiceContainer, force: bool = False) -> None:
        super().__init__()
        self.services = services
        self.force = force

    def run(self) -> None:
        try:
            changed = self.services.sync.sync_imdb_dataset(force=self.force, status_callback=self.status_changed.emit)
        except Exception as exc:
            self.sync_failed.emit(str(exc))
            return
        self.sync_completed.emit(changed)


class ProgressSyncWorker(QThread):
    sync_completed = Signal(object)
    sync_failed = Signal(str)

    def __init__(self, services: ServiceContainer, trakt_ids: list[int], *, dropped_only: bool = False) -> None:
        super().__init__()
        self.services = services
        self.trakt_ids = trakt_ids
        self.dropped_only = dropped_only

    def run(self) -> None:
        try:
            result = self.services.progress.sync_progress(self.trakt_ids, dropped_only=self.dropped_only)
        except Exception as exc:
            self.sync_failed.emit(str(exc))
            return
        self.sync_completed.emit(result)


class HistoryAutoSyncWorker(QThread):
    sync_completed = Signal(bool)
    sync_failed = Signal(str)

    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services

    def run(self) -> None:
        try:
            changed = self.services.sync.maybe_refresh_history()
        except Exception as exc:
            self.sync_failed.emit(str(exc))
            return
        self.sync_completed.emit(bool(changed))


class HistorySyncWorker(QThread):
    sync_completed = Signal()
    sync_failed = Signal(str)

    def __init__(self, services: ServiceContainer) -> None:
        super().__init__()
        self.services = services

    def run(self) -> None:
        try:
            self.services.sync.refresh_history()
        except Exception as exc:
            self.sync_failed.emit(str(exc))
            return
        self.sync_completed.emit()


class MainWindow(QMainWindow):
    _ALL_HISTORY_TITLES = "All titles"

    def __init__(self, services: ServiceContainer, startup_profiler: StartupProfiler | None = None) -> None:
        super().__init__()
        self.services = services
        self._startup_profiler = startup_profiler
        self._startup_finished = False
        self._app_icon = load_app_icon()
        scaled_font = self.font()
        scaled_font.setPointSizeF(max(10.0, scaled_font.pointSizeF() * 1.25))
        self.setFont(scaled_font)
        self.setWindowTitle("Trakt Tracker")
        self.setWindowIcon(self._app_icon)
        self.resize(self.services.auth.config.window_width, self.services.auth.config.window_height)
        if self.services.auth.config.window_x is not None and self.services.auth.config.window_y is not None:
            self.move(self.services.auth.config.window_x, self.services.auth.config.window_y)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._window_state_timer = QTimer(self)
        self._window_state_timer.setSingleShot(True)
        self._window_state_timer.timeout.connect(self._persist_window_geometry)

        self.upcoming_list = QListWidget()
        self.search_input = QComboBox()
        self.search_input.setEditable(True)
        self.search_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.search_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.search_input.lineEdit().returnPressed.connect(self._search)
        self.search_input.activated.connect(self._search_from_history_selection)
        self.search_type = QComboBox()
        self.search_type.addItems(["all", "movie", "show"])
        self.search_sort = QComboBox()
        self.search_sort.addItems(["IMDb votes", "Trakt votes", "Alphabetical"])
        self.search_sort.currentTextChanged.connect(self._on_search_sort_changed)
        self.search_results = QListView()
        self.search_model = SearchResultsModel()
        self.search_results.setViewMode(QListView.ViewMode.ListMode)
        self.search_results.setUniformItemSizes(True)
        self.search_results.setLayoutMode(QListView.LayoutMode.Batched)
        self.search_results.setBatchSize(12)
        self.search_results.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.search_results.setSpacing(_scale_px(6))
        self.search_results.setModel(self.search_model)
        self.search_results.setStyleSheet(
            "QListView { background: #ffffff; alternate-background-color: #f7fafc; }"
            "QListView::item { border: 1px solid #d8dee6; background: #ffffff; }"
            "QListView::item:selected { background: #dbeafe; border: 1px solid #93c5fd; }"
            "QListView::item:hover { background: #f3f8ff; }"
        )
        self.search_poster = QLabel("Poster preview")
        self.search_poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.search_poster.setMinimumSize(_scale_px(260), _scale_px(390))
        self.search_poster.setStyleSheet("border: 1px solid #666; background: #202020; color: #ddd;")
        self.search_summary = QTextEdit()
        self.search_summary.setReadOnly(True)
        self.search_summary.setMinimumWidth(300)
        self._poster_disk_cache = BinaryCache("images")
        self._poster_store = PosterStore(self._poster_disk_cache, lambda: self.services.auth.config.cache_ttl_hours)
        self._poster_store.bridge.loaded.connect(self._on_poster_loaded)
        self.search_delegate = SearchItemDelegate(self._poster_store, self.search_results)
        self.search_results.setItemDelegate(self.search_delegate)
        self.search_results.selectionModel().currentChanged.connect(self._update_search_preview)
        self.search_results.verticalScrollBar().valueChanged.connect(self._maybe_fetch_more_results)
        self._search_fetch_worker: SearchFetchWorker | None = None
        self._search_worker: SearchEnrichmentWorker | None = None
        self._search_generation = 0
        self._search_tab_initialized = False
        self._pending_render_generation = 0
        self._pending_render_results: list[TitleSummary] = []
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_next_search_batch)
        self._search_resort_timer = QTimer(self)
        self._search_resort_timer.setSingleShot(True)
        self._search_resort_timer.timeout.connect(self._resort_current_search_results)
        self._progress_items: list = []
        self._progress_cards: dict[int, ProgressCard] = {}
        self._progress_unseen_episode_ids: set[int] = set()
        self._progress_scroll = QScrollArea()
        self._progress_scroll.setWidgetResizable(True)
        self._progress_cards_host = QWidget()
        self._progress_cards_layout = QGridLayout(self._progress_cards_host)
        self._progress_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._progress_cards_layout.setSpacing(10)
        self._progress_cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._progress_scroll.setWidget(self._progress_cards_host)
        self._progress_last_sync_at: datetime | None = None
        self._progress_sync_interval_seconds = 300
        self._progress_sync_worker: ProgressSyncWorker | None = None
        self._history_sync_worker: HistoryAutoSyncWorker | None = None
        self._progress_sync_target_id: int | None = None
        self._player_windows: list[PlayerWindow] = []
        self.hide_upcoming_checkbox = QCheckBox("Hide Upcoming")
        self.show_dropped_checkbox = QCheckBox("Show Dropped")
        self.progress_year_filter_checkbox = QCheckBox("Year Filter")
        self.progress_min_year_spin = QSpinBox()
        self.progress_min_year_spin.setRange(1900, 3000)
        self.progress_min_year_spin.setFixedWidth(_scale_px(92))
        self.progress_min_year_spin.setKeyboardTracking(False)
        self.progress_min_year_spin.setAccelerated(True)
        self.history_type = QComboBox()
        self.history_type.addItems(["all", "movie", "show"])
        self.history_title_filter = QComboBox()
        self.history_title_filter.setEditable(True)
        self.history_title_filter.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.history_title_filter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.history_title_filter.addItem(self._ALL_HISTORY_TITLES)
        self.history_title_filter.setCurrentIndex(0)
        if self.history_title_filter.lineEdit() is not None:
            self.history_title_filter.lineEdit().setPlaceholderText("Choose title")
        completer = self.history_title_filter.completer()
        if completer is not None:
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.history_list = QTableWidget(0, 7)
        self.history_list.setHorizontalHeaderLabels(["Date/Time", "Title", "Season", "Ep", "Episode Title", "Rating", "IMDb"])
        self.history_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.history_list.setAlternatingRowColors(True)
        self.history_list.setSortingEnabled(False)
        self.history_list.verticalHeader().setVisible(False)
        header = self.history_list.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self.history_list.setColumnWidth(0, _scale_px(132))
        self.history_list.setColumnWidth(1, _scale_px(178))
        self.history_list.setColumnWidth(2, _scale_px(54))
        self.history_list.setColumnWidth(3, _scale_px(54))
        self.history_list.setColumnWidth(5, _scale_px(62))
        self.history_list.setColumnWidth(6, _scale_px(96))
        self.history_list.verticalHeader().setDefaultSectionSize(_scale_px(34))
        self._history_batch_size = 50
        self._history_loaded_count = 0
        self._history_rows_cache: list[dict] = []
        self._history_has_more = False
        self._history_sort_column = 0
        self._history_sort_order = Qt.SortOrder.DescendingOrder
        self._history_manual_sync_worker: HistorySyncWorker | None = None
        header.sortIndicatorChanged.connect(self._on_history_sort_changed)
        self.history_list.verticalScrollBar().valueChanged.connect(self._maybe_fetch_more_history_rows)
        self.history_type.currentTextChanged.connect(self._on_history_filter_changed)
        self.history_title_filter.currentTextChanged.connect(self._refresh_history)

        self.client_id_edit = QLineEdit()
        self.client_secret_edit = QLineEdit()
        self.client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.redirect_uri_edit = QLineEdit()
        self.tmdb_token_edit = QLineEdit()
        self.tmdb_api_key_edit = QLineEdit()
        self.kinopoisk_api_key_edit = QLineEdit()
        self.embedded_player_checkbox = QCheckBox("Open Kinopoisk in embedded player")
        self.cache_ttl_edit = QSpinBox()
        self.cache_ttl_edit.setRange(1, 168)
        self.poll_interval_edit = QSpinBox()
        self.poll_interval_edit.setRange(5, 240)
        self.utc_offset_edit = QLineEdit()
        self.notifications_checkbox = QCheckBox("Enable Windows notifications")
        self.debug_mode_checkbox = QCheckBox("Enable debug toasts")
        self.imdb_status_label = QLabel()
        self._imdb_sync_worker: IMDbDatasetSyncWorker | None = None

        self._build_progress_tab()
        self._build_history_tab()
        self._build_search_tab()
        self._build_upcoming_tab()
        self._build_settings_tab()
        self._apply_desktop_scale_style()
        self._play_prompt_cards: dict[int, PlayWatchPromptCard] = {}
        self._play_prompt_stack = QWidget(self)
        self._play_prompt_stack_layout = QVBoxLayout(self._play_prompt_stack)
        self._play_prompt_stack_layout.setContentsMargins(0, 0, 0, 0)
        self._play_prompt_stack_layout.setSpacing(_scale_px(10))
        self._play_prompt_stack.hide()
        self._debug_toast_stack = QWidget(self)
        self._debug_toast_stack_layout = QVBoxLayout(self._debug_toast_stack)
        self._debug_toast_stack_layout.setContentsMargins(0, 0, 0, 0)
        self._debug_toast_stack_layout.setSpacing(_scale_px(8))
        self._debug_toast_stack_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._debug_toast_stack.hide()

        self._tray_icon = QSystemTrayIcon(self)
        self._tray_icon.setIcon(self._app_icon)
        self._tray_icon.setVisible(True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_notifications)
        self._reload_settings()
        self._mark_startup("settings loaded")
        self._reload_search_history()
        self._mark_startup("search history loaded")
        self._start_timer()
        self._mark_startup("notification timer started")
        self.tabs.setCurrentIndex(self._progress_tab_index)
        QTimer.singleShot(0, self.refresh_all)
        if self.services.auth.config.window_maximized:
            QTimer.singleShot(0, self.showMaximized)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._mark_startup("main window shown")
        QTimer.singleShot(0, self._reflow_progress_cards)
        QTimer.singleShot(0, self._update_play_prompt_stack_geometry)
        QTimer.singleShot(0, self._update_debug_toast_stack_geometry)
        if not self.services.auth.is_configured():
            self._show_onboarding()

    def _apply_desktop_scale_style(self) -> None:
        self.setStyleSheet(
            """
            QTabWidget::pane {
                border: 1px solid #d7d2c7;
                top: -1px;
                background: #ffffff;
            }
            QTabBar::tab {
                min-height: 24px;
                padding: 6px 16px;
                margin-right: 2px;
                border: 1px solid #d7d2c7;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                background: #f2efe8;
                font-size: 15px;
                color: #1d1d1d;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                margin-bottom: -1px;
            }
            QTabBar::tab:!selected {
                background: #f2efe8;
            }
            QPushButton,
            QLineEdit,
            QComboBox,
            QSpinBox,
            QDateTimeEdit,
            QTextEdit,
            QListWidget,
            QTableWidget,
            QListView,
            QCheckBox {
                font-size: 15px;
            }
            QPushButton,
            QComboBox,
            QLineEdit,
            QSpinBox,
            QDateTimeEdit {
                min-height: 38px;
            }
            QListWidget,
            QTableWidget,
            QTextEdit,
            QListView {
                font-size: 14px;
            }
            QLabel {
                font-size: 14px;
            }
            """
        )

    def _queue_play_watch_prompt(self, trakt_id: int) -> None:
        current = next((item for item in self._progress_items if item.trakt_id == trakt_id), None)
        if current is None or current.next_episode is None or current.is_dropped:
            return
        episode = current.next_episode
        prompt = self._play_prompt_cards.get(trakt_id)
        episode_label = f"S{episode.season:02d}E{episode.number:02d} {episode.title}"
        if prompt is not None:
            self._dismiss_play_watch_prompt(trakt_id)
        prompt = PlayWatchPromptCard(
            trakt_id=trakt_id,
            title=current.title,
            episode_label=episode_label,
            on_watch=self._watch_from_prompt,
            on_dismiss=self._dismiss_play_watch_prompt,
            parent=self._play_prompt_stack,
        )
        self._play_prompt_cards[trakt_id] = prompt
        self._play_prompt_stack_layout.addWidget(prompt)
        self._play_prompt_stack.show()
        self._update_play_prompt_stack_geometry()

    def _watch_from_prompt(self, trakt_id: int) -> None:
        self._mark_progress_episode_watched(trakt_id)
        self._dismiss_play_watch_prompt(trakt_id)

    def _dismiss_play_watch_prompt(self, trakt_id: int) -> None:
        prompt = self._play_prompt_cards.pop(trakt_id, None)
        if prompt is None:
            return
        self._play_prompt_stack_layout.removeWidget(prompt)
        prompt.deleteLater()
        if not self._play_prompt_cards:
            self._play_prompt_stack.hide()
        self._update_play_prompt_stack_geometry()

    def _update_play_prompt_stack_geometry(self) -> None:
        if not hasattr(self, "_play_prompt_stack"):
            return
        margin = _scale_px(18)
        width = min(_scale_px(360), max(_scale_px(280), self.width() - margin * 2))
        height = self._play_prompt_stack.sizeHint().height()
        self._play_prompt_stack.setGeometry(
            max(margin, (self.width() - width) // 2),
            max(margin, (self.height() - height) // 2),
            width,
            height,
        )
        self._play_prompt_stack.raise_()

    def _on_tab_changed(self, index: int) -> None:
        tab_name = self.tabs.tabText(index)
        if tab_name == "Search" and not self._search_tab_initialized:
            self._search_tab_initialized = True
            QTimer.singleShot(0, self._restore_last_search)
        if tab_name == "History":
            QTimer.singleShot(0, self._maybe_auto_sync_history)
        if tab_name == "Progress":
            QTimer.singleShot(0, self._reflow_progress_cards)
            QTimer.singleShot(0, self._maybe_auto_sync_progress)

    def _build_search_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._search)
        open_btn = QPushButton("Open details")
        open_btn.clicked.connect(self._open_selected_search_result)
        rate_btn = QPushButton("Rate")
        rate_btn.clicked.connect(self._rate_selected_search_result)
        history_btn = QPushButton("Add to history")
        history_btn.clicked.connect(self._add_selected_search_result_to_history)

        row = QHBoxLayout()
        row.addWidget(self.search_input)
        row.addWidget(self.search_type)
        row.addWidget(self.search_sort)
        row.addWidget(search_btn)
        row.addWidget(open_btn)
        row.addWidget(rate_btn)
        row.addWidget(history_btn)

        layout.addLayout(row)
        content_row = QHBoxLayout()
        self.search_results.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        content_row.addWidget(self.search_results, 4)

        preview_col = QVBoxLayout()
        preview_col.addWidget(self.search_poster)
        preview_col.addWidget(self.search_summary, 1)
        content_row.addLayout(preview_col, 1)

        layout.addLayout(content_row)
        self.tabs.addTab(page, "Search")

    def _build_history_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        sync_btn = QPushButton("Sync")
        sync_btn.clicked.connect(self._sync_and_refresh_history)
        row.addWidget(sync_btn)
        row.addWidget(self.history_type)
        row.addWidget(self.history_title_filter, 1)
        row.addStretch()

        layout.addLayout(row)
        layout.addWidget(self.history_list)
        self.tabs.addTab(page, "History")

    def _build_progress_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        sync_btn = QPushButton("Sync")
        sync_btn.clicked.connect(lambda: self._sync_progress(force=True, full=True))
        self.hide_upcoming_checkbox.toggled.connect(self._on_progress_filter_changed)
        self.show_dropped_checkbox.toggled.connect(self._on_progress_filter_changed)
        self.progress_year_filter_checkbox.toggled.connect(self._on_progress_filter_changed)
        self.progress_min_year_spin.editingFinished.connect(self._on_progress_year_changed)

        row = QHBoxLayout()
        row.addWidget(sync_btn)
        row.addWidget(self.hide_upcoming_checkbox)
        row.addWidget(self.show_dropped_checkbox)
        row.addWidget(self.progress_year_filter_checkbox)
        row.addWidget(self.progress_min_year_spin)
        row.addStretch()

        layout.addLayout(row)
        layout.addWidget(self._progress_scroll)
        self._progress_tab_index = self.tabs.addTab(page, "Progress")

    def _build_upcoming_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        refresh_btn = QPushButton("Refresh upcoming")
        refresh_btn.clicked.connect(self._refresh_upcoming)
        poll_btn = QPushButton("Poll notifications now")
        poll_btn.clicked.connect(self._poll_notifications)
        row = QHBoxLayout()
        row.addWidget(refresh_btn)
        row.addWidget(poll_btn)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(self.upcoming_list)
        self.tabs.addTab(page, "Upcoming")

    def _build_settings_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        auth_btn = QPushButton("Authorize with Trakt")
        auth_btn.clicked.connect(self._authorize)
        full_resync_btn = QPushButton("Full resync")
        full_resync_btn.clicked.connect(self._run_initial_import)
        clear_trakt_cache_btn = QPushButton("Clear Trakt cache")
        clear_trakt_cache_btn.clicked.connect(lambda: self._clear_cache("trakt"))
        clear_tmdb_cache_btn = QPushButton("Clear TMDb cache")
        clear_tmdb_cache_btn.clicked.connect(lambda: self._clear_cache("tmdb"))
        sync_imdb_btn = QPushButton("Sync IMDb dataset")
        sync_imdb_btn.clicked.connect(lambda: self._sync_imdb_dataset(force=True))
        clear_imdb_btn = QPushButton("Clear IMDb dataset")
        clear_imdb_btn.clicked.connect(self._clear_imdb_dataset)

        self.client_id_edit.setMaximumWidth(320)
        self.client_secret_edit.setMaximumWidth(320)
        self.redirect_uri_edit.setMaximumWidth(360)
        self.tmdb_token_edit.setMaximumWidth(420)
        self.tmdb_api_key_edit.setMaximumWidth(260)
        self.kinopoisk_api_key_edit.setMaximumWidth(320)
        self.utc_offset_edit.setMaximumWidth(100)

        def inline_row(*widgets: QWidget) -> QWidget:
            wrapper = QWidget()
            row = QHBoxLayout(wrapper)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            for widget in widgets:
                row.addWidget(widget)
            row.addStretch()
            return wrapper

        form.addRow("Client ID", inline_row(self.client_id_edit, auth_btn))
        form.addRow("Client Secret", inline_row(self.client_secret_edit))
        form.addRow("Redirect URI", inline_row(self.redirect_uri_edit, full_resync_btn))
        form.addRow("TMDb Read Token", inline_row(self.tmdb_token_edit, clear_tmdb_cache_btn))
        form.addRow("TMDb API Key", inline_row(self.tmdb_api_key_edit))
        form.addRow("Kinopoisk API Key", inline_row(self.kinopoisk_api_key_edit))
        form.addRow("Playback", inline_row(self.embedded_player_checkbox))
        form.addRow("UTC offset", inline_row(self.utc_offset_edit))
        form.addRow("IMDb Dataset", inline_row(self.imdb_status_label, sync_imdb_btn, clear_imdb_btn))
        form.addRow("Cache TTL (hours)", inline_row(self.cache_ttl_edit, clear_trakt_cache_btn))
        form.addRow("Polling interval (minutes)", inline_row(self.poll_interval_edit, self.notifications_checkbox, self.debug_mode_checkbox))

        save_row = QHBoxLayout()
        save_btn = QPushButton("Save settings")
        save_btn.clicked.connect(self._save_settings)
        save_row.addWidget(save_btn)
        save_row.addStretch()

        layout.addLayout(form)
        layout.addLayout(save_row)
        self.tabs.addTab(page, "Settings")

    def _show_onboarding(self) -> None:
        dialog = OnboardingDialog(self.services, self)
        if dialog.exec():
            self._reload_settings()

    def _reload_settings(self) -> None:
        config = self.services.auth.config
        self.client_id_edit.setText(config.client_id)
        self.client_secret_edit.setText(config.client_secret)
        self.redirect_uri_edit.setText(config.redirect_uri)
        self.tmdb_token_edit.setText(config.tmdb_read_access_token)
        self.tmdb_api_key_edit.setText(config.tmdb_api_key)
        self.kinopoisk_api_key_edit.setText(config.kinopoisk_api_key)
        self.utc_offset_edit.setText(config.utc_offset)
        self.embedded_player_checkbox.setChecked(config.open_in_embedded_player)
        self.hide_upcoming_checkbox.blockSignals(True)
        self.hide_upcoming_checkbox.setChecked(config.hide_upcoming_in_progress)
        self.hide_upcoming_checkbox.blockSignals(False)
        self.show_dropped_checkbox.blockSignals(True)
        self.show_dropped_checkbox.setChecked(config.show_dropped_in_progress)
        self.show_dropped_checkbox.blockSignals(False)
        self.progress_year_filter_checkbox.blockSignals(True)
        self.progress_year_filter_checkbox.setChecked(config.web_progress_year_filter_enabled)
        self.progress_year_filter_checkbox.blockSignals(False)
        self.progress_min_year_spin.blockSignals(True)
        self.progress_min_year_spin.setValue(config.web_progress_min_year or 2024)
        self.progress_min_year_spin.blockSignals(False)
        self.cache_ttl_edit.setValue(config.cache_ttl_hours)
        self.poll_interval_edit.setValue(config.poll_interval_minutes)
        self.notifications_checkbox.setChecked(config.notifications_enabled)
        self.debug_mode_checkbox.setChecked(config.debug_mode)
        self.imdb_status_label.setText(self.services.sync.imdb_dataset_status())
        sort_mode = self.services.catalog.get_search_sort_mode()
        index = self.search_sort.findText(sort_mode)
        self.search_sort.blockSignals(True)
        self.search_sort.setCurrentIndex(index if index >= 0 else 0)
        self.search_sort.blockSignals(False)

    def _reload_search_history(self) -> None:
        current = self.search_input.currentText().strip()
        history = self.services.catalog.search_history()
        self.search_input.blockSignals(True)
        self.search_input.clear()
        for item in history:
            self.search_input.addItem(item)
        if current:
            self.search_input.setCurrentText(current)
        elif history:
            self.search_input.setCurrentText(history[0])
        self.search_input.blockSignals(False)

    def _restore_last_search(self) -> None:
        state = self.services.catalog.load_last_search_state()
        if not state:
            return
        query = state.get("query", "").strip()
        title_type = state.get("title_type", "all")
        sort_mode = self.services.catalog.get_search_sort_mode() or state.get("sort_mode", "IMDb votes")
        results = state.get("results", [])
        if query:
            self.search_input.setCurrentText(query)
        index = self.search_type.findText(title_type)
        if index >= 0:
            self.search_type.setCurrentIndex(index)
        sort_index = self.search_sort.findText(sort_mode)
        if sort_index >= 0:
            self.search_sort.blockSignals(True)
            self.search_sort.setCurrentIndex(sort_index)
            self.search_sort.blockSignals(False)
        if results:
            self._queue_search_results(self._sort_search_results(results), self._search_generation)
            self._start_search_enrichment(results, self._search_generation)

    def _save_settings(self) -> None:
        config = self.services.auth.update_config(
            self.client_id_edit.text(),
            self.client_secret_edit.text(),
            self.redirect_uri_edit.text(),
            self.tmdb_api_key_edit.text(),
            self.tmdb_token_edit.text(),
            self.kinopoisk_api_key_edit.text(),
        )
        config.cache_ttl_hours = self.cache_ttl_edit.value()
        config.poll_interval_minutes = self.poll_interval_edit.value()
        config.notifications_enabled = self.notifications_checkbox.isChecked()
        config.debug_mode = self.debug_mode_checkbox.isChecked()
        config.open_in_embedded_player = self.embedded_player_checkbox.isChecked()
        config.utc_offset = normalize_utc_offset(self.utc_offset_edit.text(), config.utc_offset)
        ConfigStore().save(config)
        self._start_timer()
        QMessageBox.information(self, "Saved", "Settings saved.")
        self._debug_toast("Settings saved.")

    def _clear_cache(self, provider: str) -> None:
        self.services.cache.clear_provider(provider)
        QMessageBox.information(self, "Cache cleared", f"{provider.upper()} cache cleared.")

    def _clear_imdb_dataset(self) -> None:
        self.services.sync.clear_imdb_dataset()
        self.imdb_status_label.setText(self.services.sync.imdb_dataset_status())
        QMessageBox.information(self, "IMDb cleared", "IMDb dataset cleared.")

    def _authorize(self) -> None:
        try:
            slug = self.services.auth.authorize()
            QMessageBox.information(self, "Authorized", f"Signed in as {slug}.")
        except Exception as exc:  # pragma: no cover - UI feedback
            QMessageBox.critical(self, "Authorization failed", str(exc))

    def _run_initial_import(self) -> None:
        try:
            self.services.sync.initial_import()
            self.refresh_all()
            QMessageBox.information(self, "Sync complete", "Initial import completed.")
        except Exception as exc:  # pragma: no cover - UI feedback
            QMessageBox.critical(self, "Sync failed", str(exc))

    def _search(self) -> None:
        query = self.search_input.currentText().strip()
        if not query:
            return
        title_type = self.search_type.currentText()
        if title_type == "all":
            title_type = None
        self._reload_search_history()
        self.search_input.setCurrentText(query)
        state = self.services.catalog.load_last_search_state()
        if (
            state
            and state.get("query", "").strip() == query
            and (None if state.get("title_type") == "all" else state.get("title_type")) == title_type
            and state.get("results")
        ):
            self._search_generation += 1
            generation = self._search_generation
            cached_results = self._sort_search_results(list(state.get("results", [])))
            self._queue_search_results(cached_results, generation)
            self._start_search_enrichment(cached_results, generation)
            return
        self._search_generation += 1
        generation = self._search_generation
        self.search_model.set_results([])
        self.search_poster.setText("Loading...")
        self.search_poster.setPixmap(QPixmap())
        self.search_summary.setPlainText("Loading search results...")
        self._start_search_fetch(query, title_type, generation)

    def _search_from_history_selection(self) -> None:
        if self.search_input.currentText().strip():
            self._search()

    def _on_search_sort_changed(self, mode: str) -> None:
        self.services.catalog.set_search_sort_mode(mode)
        current_results = list(self.search_model._all_results)
        if current_results:
            self.search_model.replace_results(self._sort_search_results(current_results))
            self._persist_current_search_state()

    def _start_search_fetch(self, query: str, title_type: str | None, generation: int) -> None:
        if self._search_fetch_worker is not None and self._search_fetch_worker.isRunning():
            self._search_fetch_worker.requestInterruption()
            self._search_fetch_worker.wait(100)
        self._search_fetch_worker = SearchFetchWorker(self.services, generation, query, title_type)
        self._search_fetch_worker.search_completed.connect(self._handle_search_completed)
        self._search_fetch_worker.search_failed.connect(self._handle_search_failed)
        self._search_fetch_worker.start()

    def _handle_search_completed(self, generation: int, results: list[TitleSummary]) -> None:
        if generation != self._search_generation:
            return
        results = self._sort_search_results(results)
        self._queue_search_results(results, generation)
        self._start_search_enrichment(results, generation)

    def _handle_search_failed(self, generation: int, message: str) -> None:
        if generation != self._search_generation:
            return
        self.search_poster.setText("Poster preview")
        self.search_summary.clear()
        QMessageBox.critical(self, "Search failed", message)

    def _queue_search_results(self, results: list[TitleSummary], generation: int) -> None:
        if generation != self._search_generation:
            return
        self._render_timer.stop()
        self._pending_render_generation = generation
        self._pending_render_results = list(results)
        self.search_model.set_results([])
        self.search_poster.setText("Poster preview")
        self.search_poster.setPixmap(QPixmap())
        self.search_summary.clear()
        self._render_timer.start(0)

    def _render_next_search_batch(self) -> None:
        generation = self._pending_render_generation
        if generation != self._search_generation:
            self._pending_render_results.clear()
            return
        batch = self._pending_render_results[:8]
        self._pending_render_results = self._pending_render_results[8:]
        self.search_model.append_results(batch)
        if self.search_model.loaded_count() > 0 and not self.search_results.currentIndex().isValid():
            self.search_results.setCurrentIndex(self.search_model.index(0, 0))

    def _sort_search_results(self, results: list[TitleSummary]) -> list[TitleSummary]:
        mode = self.search_sort.currentText()
        if mode == "IMDb votes":
            return sorted(
                results,
                key=lambda item: (item.imdb_votes or 0, item.imdb_rating or 0.0, (item.title or "").lower()),
                reverse=True,
            )
        if mode == "Alphabetical":
            return sorted(results, key=lambda item: ((item.title or "").lower(), item.year or 0))
        return sorted(
            results,
            key=lambda item: (item.trakt_votes or 0, item.trakt_rating or 0.0, (item.title or "").lower()),
            reverse=True,
        )

    def _start_search_enrichment(self, results: list[TitleSummary], generation: int) -> None:
        if self._search_worker is not None and self._search_worker.isRunning():
            self._search_worker.requestInterruption()
            self._search_worker.wait(100)
        config = self.services.auth.config
        if not (config.tmdb_read_access_token or config.tmdb_api_key):
            return
        self._search_worker = SearchEnrichmentWorker(self.services, results)
        self._search_worker.item_enriched.connect(
            lambda index, title: self._apply_enriched_result(index, title, generation)
        )
        self._search_worker.start()

    def _apply_enriched_result(self, index: int, title: TitleSummary, generation: int) -> None:
        if generation != self._search_generation:
            return
        model_index = self.search_model.index(index, 0)
        if not model_index.isValid():
            return
        self.search_model.update_title(index, title)
        if self.search_results.currentIndex() == model_index:
            self._update_search_preview(model_index, QModelIndex())
        self._search_resort_timer.start(120)

    def _resort_current_search_results(self) -> None:
        current_results = list(self.search_model._all_results)
        if not current_results:
            return
        current_title = self._selected_title()
        selected_key = (current_title.trakt_id, current_title.title_type) if current_title is not None else None
        sorted_results = self._sort_search_results(current_results)
        self.search_model.replace_results(sorted_results)
        if selected_key is not None:
            for row, item in enumerate(sorted_results[: self.search_model.loaded_count()]):
                if (item.trakt_id, item.title_type) == selected_key:
                    self.search_results.setCurrentIndex(self.search_model.index(row, 0))
                    break
        self._persist_current_search_state()

    def _persist_current_search_state(self) -> None:
        query = self.search_input.currentText().strip()
        if not query:
            return
        title_type = self.search_type.currentText()
        self.services.catalog.save_last_search_state(
            query,
            None if title_type == "all" else title_type,
            list(self.search_model._all_results),
        )

    def _selected_title(self) -> TitleSummary | None:
        index = self.search_results.currentIndex()
        if not index.isValid():
            return None
        return index.data(SearchResultsModel.TitleRole)

    def _update_search_preview(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            self.search_poster.setText("Poster preview")
            self.search_poster.setPixmap(QPixmap())
            self.search_summary.clear()
            return
        title: TitleSummary = current.data(SearchResultsModel.TitleRole)
        self.search_summary.setPlainText(
            "\n".join(
                [
                    f"{title.title} ({title.year or 'n/a'})",
                    f"Type: {title.title_type}",
                    f"Status: {title.status or 'n/a'}",
                    f"Trakt: {_format_rating_with_votes(title.trakt_rating, title.trakt_votes)}",
                    f"TMDb: {_format_rating_with_votes(title.tmdb_rating, title.tmdb_votes)}",
                    (
                        f"IMDb: {_format_rating_with_votes(title.imdb_rating, title.imdb_votes)}"
                        if title.imdb_rating is not None
                        else f"IMDb: {title.imdb_id or 'n/a'}"
                    ),
                    "",
                    title.overview or "No overview.",
                ]
            )
        )
        self._set_search_poster(title.poster_url)
        self._maybe_fetch_more_results()

    def _set_search_poster(self, poster_url: str) -> None:
        if not poster_url:
            self.search_poster.setPixmap(QPixmap())
            self.search_poster.setText("No poster")
            return
        pixmap = self._poster_store.get(poster_url)
        if pixmap is None or pixmap.isNull():
            if not self._poster_store.is_failed(poster_url):
                self._poster_store.request(poster_url)
            self.search_poster.setPixmap(QPixmap())
            self.search_poster.setText("No poster" if self._poster_store.is_failed(poster_url) else "Loading...")
            return
        self.search_poster.setText("")
        self.search_poster.setPixmap(
            pixmap.scaled(
                self.search_poster.width(),
                self.search_poster.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _on_poster_loaded(self, poster_url: str) -> None:
        rows = self.search_model.rows_for_poster(poster_url)
        for row in rows:
            index = self.search_model.index(row, 0)
            self.search_model.dataChanged.emit(index, index, [SearchResultsModel.TitleRole])
        current = self.search_results.currentIndex()
        if current.isValid():
            title = current.data(SearchResultsModel.TitleRole)
            if isinstance(title, TitleSummary) and title.poster_url == poster_url:
                self._set_search_poster(poster_url)
        for progress in self._progress_items:
            if progress.poster_url != poster_url:
                continue
            card = self._progress_cards.get(progress.trakt_id)
            if card is None:
                continue
            pixmap = self._poster_store.get(poster_url)
            if pixmap is not None and not pixmap.isNull():
                card.set_pixmap(pixmap)

    def _maybe_fetch_more_results(self, *_args) -> None:
        if self.search_model.total_count() == 0:
            if self._pending_render_results and not self._render_timer.isActive():
                self._render_timer.start(0)
            return
        current = self.search_results.currentIndex()
        if current.isValid() and current.row() >= self.search_model.loaded_count() - 3 and self._pending_render_results and not self._render_timer.isActive():
            self._render_timer.start(0)
        if current.isValid() and current.row() >= self.search_model.loaded_count() - 3 and self.search_model.canFetchMore():
            self.search_model.fetchMore()
        scroll = self.search_results.verticalScrollBar()
        if scroll.maximum() > 0 and scroll.value() >= scroll.maximum() - 200 and self._pending_render_results and not self._render_timer.isActive():
            self._render_timer.start(0)
        if scroll.maximum() > 0 and scroll.value() >= scroll.maximum() - 200 and self.search_model.canFetchMore():
            self.search_model.fetchMore()

    def _open_selected_search_result(self) -> None:
        title = self._selected_title()
        if title is None:
            return
        dialog = TitleDetailsDialog(self.services, title, self)
        dialog.exec()
        self.refresh_all()

    def _rate_selected_search_result(self) -> None:
        title = self._selected_title()
        if title is None:
            return
        dialog = RatingDialog(title.title, self)
        if dialog.exec():
            self.services.library.set_rating(
                RatingInput(title_type=title.title_type, trakt_id=title.trakt_id, rating=dialog.rating.value()),
                title=title.title,
            )
            self._refresh_history()

    def _add_selected_search_result_to_history(self) -> None:
        title = self._selected_title()
        if title is None:
            return
        dialog = HistoryDialog(title, self)
        if dialog.exec():
            self.services.library.add_history_item(dialog.to_input())
            if title.title_type == "show":
                self.services.progress.refresh_show_progress(title.trakt_id)
            self.refresh_all()

    def _clear_progress_cards(self) -> None:
        self._progress_cards.clear()
        while self._progress_cards_layout.count() > 0:
            item = self._progress_cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _progress_column_count(self) -> int:
        width_candidates = [
            self._progress_scroll.viewport().width(),
            self._progress_scroll.width() - 24,
            self.width() - 80,
        ]
        width = max(320, max(width_candidates))
        return max(1, width // 530)

    def _filtered_progress_items(self, items: list) -> list:
        filtered = items
        if not self.show_dropped_checkbox.isChecked() and self.hide_upcoming_checkbox.isChecked():
            filtered = [item for item in filtered if item.completed < _effective_progress_aired(item)]
        if self.progress_year_filter_checkbox.isChecked():
            min_year = self.progress_min_year_spin.value()
            filtered = [
                item
                for item in filtered
                if item.next_episode is not None
                and item.next_episode.first_aired is not None
                and item.next_episode.first_aired.year >= min_year
            ]
        return filtered

    def _reflow_progress_cards(self) -> None:
        if self._progress_items:
            self._render_progress_cards(self._progress_items)

    def _refresh_progress(self, focus_trakt_id: int | None = None) -> None:
        self._progress_items = self._filtered_progress_items(
            self.services.progress.dashboard_progress(dropped_only=self.show_dropped_checkbox.isChecked())
        )
        self._progress_unseen_episode_ids = self.services.notifications.unseen_episode_ids()
        self._render_progress_cards(self._progress_items, focus_trakt_id=focus_trakt_id)

    def _render_progress_cards(self, items: list, focus_trakt_id: int | None = None) -> None:
        self._clear_progress_cards()
        if not items:
            empty_text = "No active shows with a next episode to watch."
            if self.hide_upcoming_checkbox.isChecked():
                empty_text = "No currently unfinished shows after hiding upcoming-only titles."
            empty = QLabel(empty_text)
            empty.setStyleSheet("color: #5a5a5a; padding: 12px;")
            self._progress_cards_layout.addWidget(empty, 0, 0)
            return
        ordered_items = list(items)
        if focus_trakt_id is not None:
            ordered_items.sort(key=lambda item: 0 if item.trakt_id == focus_trakt_id else 1)
        columns = self._progress_column_count()
        new_items = [
            item for item in ordered_items
            if item.next_episode is not None and item.next_episode.trakt_id in self._progress_unseen_episode_ids
        ]
        remaining_items = [
            item for item in ordered_items
            if item.next_episode is None or item.next_episode.trakt_id not in self._progress_unseen_episode_ids
        ]
        sections: list[tuple[str, list]] = []
        if new_items:
            sections.append(("New", new_items))
        if remaining_items:
            sections.append(("Progress", remaining_items))
        row_offset = 0
        for section_title, section_items in sections:
            heading = QLabel(section_title)
            heading.setStyleSheet("font-size: 24px; font-weight: 700; color: #111111; padding: 8px 4px 4px 4px;")
            self._progress_cards_layout.addWidget(heading, row_offset, 0, 1, columns)
            row_offset += 1
            for index, item in enumerate(section_items):
                row = row_offset + (index // columns)
                column = index % columns
                next_episode_id = item.next_episode.trakt_id if item.next_episode is not None else None
                card = ProgressCard(
                    item,
                    self._open_progress_play,
                    self._mark_progress_episode_watched,
                    self._toggle_progress_drop,
                    utc_offset=self.services.auth.config.utc_offset,
                    is_unseen_release=bool(next_episode_id and next_episode_id in self._progress_unseen_episode_ids),
                    on_open_new=self._mark_progress_episode_seen,
                )
                self._progress_cards[item.trakt_id] = card
                self._progress_cards_layout.addWidget(card, row, column)
                pixmap = self._poster_store.get(item.poster_url)
                if pixmap is not None and not pixmap.isNull():
                    card.set_pixmap(pixmap)
                else:
                    if item.poster_url and not self._poster_store.is_failed(item.poster_url):
                        self._poster_store.request(item.poster_url)
                    card.set_loading_state(failed=(not item.poster_url) or self._poster_store.is_failed(item.poster_url))
            row_offset += (len(section_items) + columns - 1) // columns
        for column in range(columns):
            self._progress_cards_layout.setColumnStretch(column, 1)

    def _visible_progress_ids(self) -> list[int]:
        if not self._progress_items:
            return []
        visible_cards = max(3, self._progress_column_count() * 2)
        return [item.trakt_id for item in self._progress_items[:visible_cards]]

    def _on_progress_filter_changed(self, _checked: bool) -> None:
        self.services.auth.config.hide_upcoming_in_progress = self.hide_upcoming_checkbox.isChecked()
        self.services.auth.config.show_dropped_in_progress = self.show_dropped_checkbox.isChecked()
        self.services.auth.config.web_progress_year_filter_enabled = self.progress_year_filter_checkbox.isChecked()
        self.services.auth.config.web_progress_min_year = self.progress_min_year_spin.value()
        ConfigStore().save(self.services.auth.config)
        self._refresh_progress()

    def _on_progress_year_changed(self) -> None:
        self.services.auth.config.web_progress_min_year = self.progress_min_year_spin.value()
        ConfigStore().save(self.services.auth.config)
        if self.progress_year_filter_checkbox.isChecked():
            self._refresh_progress()

    def _sync_progress(self, force: bool = False, focus_trakt_id: int | None = None, full: bool = False) -> None:
        now = datetime.now()
        if not force and self._progress_last_sync_at is not None:
            if (now - self._progress_last_sync_at).total_seconds() < self._progress_sync_interval_seconds:
                self._refresh_progress(focus_trakt_id=focus_trakt_id)
                return
        if self._progress_sync_worker is not None and self._progress_sync_worker.isRunning():
            return
        if focus_trakt_id is not None:
            trakt_ids = [focus_trakt_id]
        elif full:
            trakt_ids = [item.trakt_id for item in self._progress_items]
        else:
            trakt_ids = self._visible_progress_ids()
        if not trakt_ids:
            self._refresh_progress(focus_trakt_id=focus_trakt_id)
            return
        self._progress_sync_target_id = focus_trakt_id
        self._progress_sync_worker = ProgressSyncWorker(
            self.services,
            trakt_ids,
            dropped_only=self.show_dropped_checkbox.isChecked(),
        )
        self._debug_toast(f"Progress sync: checking {len(trakt_ids)} show(s)…")
        self._progress_sync_worker.sync_completed.connect(self._on_progress_sync_completed)
        self._progress_sync_worker.sync_failed.connect(self._on_progress_sync_failed)
        self._progress_sync_worker.start()

    def _maybe_auto_sync_progress(self) -> None:
        self._sync_progress(force=False)

    def _on_progress_sync_completed(self, _result) -> None:
        self._progress_last_sync_at = datetime.now()
        focus = self._progress_sync_target_id
        self._progress_sync_target_id = None
        self._progress_sync_worker = None
        self._refresh_progress(focus_trakt_id=focus)
        self._debug_toast("Progress sync completed.")

    def _on_progress_sync_failed(self, message: str) -> None:
        focus = self._progress_sync_target_id
        self._progress_sync_target_id = None
        self._progress_sync_worker = None
        QMessageBox.critical(self, "Progress sync failed", message)
        self._refresh_progress(focus_trakt_id=focus)
        self._debug_toast(f"Progress sync failed: {message}")

    def _mark_progress_episode_watched(self, trakt_id: int) -> None:
        current = next((item for item in self._progress_items if item.trakt_id == trakt_id), None)
        if current is None or current.next_episode is None:
            return
        episode = current.next_episode
        try:
            self.services.notifications.mark_episode_seen(
                show_trakt_id=current.trakt_id,
                show_title=current.title,
                episode=episode,
            )
            self.services.library.add_history_item(
                HistoryItemInput(
                    title_type="show",
                    trakt_id=current.trakt_id,
                    watched_at=datetime.now(),
                    season=episode.season,
                    episode=episode.number,
                    title=current.title,
                )
            )
        except Exception as exc:
            QMessageBox.critical(self, "Mark watched failed", str(exc))
            return

        dialog = RatingDialog(f"{current.title} S{episode.season:02d}E{episode.number:02d}", self)
        if dialog.exec() and not dialog.skipped:
            try:
                expected_rating = dialog.rating.value()
                self.services.library.set_rating(
                    RatingInput(
                        title_type="show",
                        trakt_id=current.trakt_id,
                        rating=expected_rating,
                        season=episode.season,
                        episode=episode.number,
                    ),
                    title=current.title,
                )
                saved_rating = self.services.library.displayed_history_rating(
                    title_type="show",
                    trakt_id=current.trakt_id,
                    season=episode.season,
                    episode=episode.number,
                )
                if saved_rating != expected_rating:
                    raise RuntimeError("Rating did not appear in history after save")
            except Exception as exc:
                QMessageBox.critical(self, "Rating failed", str(exc))
                return
        self._dismiss_play_watch_prompt(trakt_id)
        self._refresh_history()
        self._sync_progress(force=True, focus_trakt_id=current.trakt_id)
        self._debug_toast(f"Marked watched: {current.title} S{episode.season:02d}E{episode.number:02d}")

    def _mark_progress_episode_seen(self, trakt_id: int) -> None:
        current = next((item for item in self._progress_items if item.trakt_id == trakt_id), None)
        if current is None or current.next_episode is None:
            return
        episode = current.next_episode
        if episode.first_aired is None:
            return
        now = datetime.now(tz=UTC)
        release_at = episode.first_aired
        if release_at.tzinfo is None:
            release_at = release_at.replace(tzinfo=UTC)
        if release_at > now:
            return
        self.services.notifications.mark_episode_seen(
            show_trakt_id=current.trakt_id,
            show_title=current.title,
            episode=episode,
        )
        self._refresh_progress(focus_trakt_id=current.trakt_id)
        self._debug_toast(f"Marked seen: {current.title} S{episode.season:02d}E{episode.number:02d}")

    def _toggle_progress_drop(self, trakt_id: int) -> None:
        current = next((item for item in self._progress_items if item.trakt_id == trakt_id), None)
        if current is None:
            return
        if not current.is_dropped:
            answer = QMessageBox.question(
                self,
                "Drop show",
                f"Drop {current.title} from Progress?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            if current.is_dropped:
                self.services.progress.undrop_show(trakt_id)
            else:
                self.services.progress.drop_show(trakt_id)
        except Exception as exc:
            QMessageBox.critical(self, "Drop failed", str(exc))
            return
        if not current.is_dropped:
            self._dismiss_play_watch_prompt(trakt_id)
        self._refresh_progress()

    def _open_progress_play(self, trakt_id: int) -> None:
        current = next((item for item in self._progress_items if item.trakt_id == trakt_id), None)
        if current is None or not current.title.strip():
            return
        try:
            target_url = self.services.play.resolve_kinopoisk_url(current.title, domain="net")
        except Exception as exc:
            QMessageBox.critical(self, "Play failed", str(exc))
            self._debug_toast(f"Play failed: {exc}")
            return
        if not target_url:
            QMessageBox.information(self, "Not found", f"Kinopoisk filmId was not found for {current.title}.")
            self._debug_toast(f"Play resolve failed: {current.title}")
            return
        self._queue_play_watch_prompt(trakt_id)
        if not self.services.auth.config.open_in_embedded_player:
            webbrowser.open(target_url)
            self._debug_toast(f"Play opened in browser: {current.title}")
            return
        player = PlayerWindow(current.title, target_url, self)
        player.destroyed.connect(lambda *_args, window=player: self._forget_player_window(window))
        self._player_windows.append(player)
        player.show()
        self._debug_toast(f"Play opened in embedded player: {current.title}")

    def _forget_player_window(self, window: PlayerWindow) -> None:
        self._player_windows = [item for item in self._player_windows if item is not window]

    def _refresh_history(self) -> None:
        title_type = self.history_type.currentText()
        if title_type == "all":
            title_type = None
        self._populate_history_title_filter(title_type)
        title_filter = self.history_title_filter.currentText().strip()
        normalized_title_filter = title_filter if title_filter and title_filter != self._ALL_HISTORY_TITLES else None
        if self._is_default_history_sort():
            self._history_rows_cache = []
            self._history_loaded_count = 0
            self._history_has_more = True
            self.history_list.setUpdatesEnabled(False)
            self.history_list.setRowCount(0)
            self._load_next_history_page(title_type, normalized_title_filter, reset=True)
            self.history_list.horizontalHeader().setSortIndicator(self._history_sort_column, self._history_sort_order)
            self.history_list.setUpdatesEnabled(True)
            return

        rows = self.services.library.history(title_type=title_type, title_filter=normalized_title_filter)
        self._history_rows_cache = self._sort_history_rows(rows)
        self._history_loaded_count = 0
        self._history_has_more = False
        self.history_list.setUpdatesEnabled(False)
        self.history_list.setRowCount(0)
        self._append_history_rows(min(self._history_batch_size, len(self._history_rows_cache)))
        self.history_list.horizontalHeader().setSortIndicator(self._history_sort_column, self._history_sort_order)
        self.history_list.setUpdatesEnabled(True)

    def _append_history_rows(self, target_count: int) -> None:
        if target_count <= self._history_loaded_count:
            return
        target_count = min(target_count, len(self._history_rows_cache))
        start_row = self._history_loaded_count
        self.history_list.setRowCount(target_count)
        for table_row in range(start_row, target_count):
            row = self._history_rows_cache[table_row]
            title_text = row["title"] or "Untitled"
            watched_at = _format_app_datetime(row["watched_at"], self.services.auth.config.utc_offset)
            season_text = f"s{row['season']:02d}" if row["type"] == "show" and row["season"] is not None else ""
            if row["type"] == "show" and row["episode"] is not None:
                episode_number_text = f"ep{row['episode']:02d}"
                episode_title_text = (row.get("episode_title") or "").strip()
            else:
                episode_number_text = ""
                episode_title_text = ""
            rating_value = row.get("display_rating")
            rating_text = f"{rating_value} ★" if rating_value is not None else ""
            imdb_rating = row.get("episode_imdb_rating")
            imdb_votes = row.get("episode_imdb_votes")
            imdb_text = ""
            if imdb_rating is not None:
                imdb_text = f"{imdb_rating:.1f}"
                compact_votes = _format_compact_votes(imdb_votes)
                if compact_votes:
                    imdb_text += f" ({compact_votes})"

            values = [watched_at, title_text, season_text, episode_number_text, episode_title_text, rating_text, imdb_text]
            for column, value in enumerate(values):
                item = SortableTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row["watched_at"].timestamp())
                elif column == 2 and row["season"] is not None:
                    item.setData(Qt.ItemDataRole.UserRole, int(row["season"]))
                elif column == 3 and row["episode"] is not None:
                    item.setData(Qt.ItemDataRole.UserRole, int(row["episode"]))
                elif column == 5 and rating_value is not None:
                    item.setData(Qt.ItemDataRole.UserRole, int(rating_value))
                elif column == 6 and imdb_rating is not None:
                    item.setData(Qt.ItemDataRole.UserRole, float(imdb_rating))
                self.history_list.setItem(table_row, column, item)
            if rating_value is None:
                rate_btn = QPushButton("Rate")
                rate_btn.clicked.connect(lambda _checked=False, row_data=dict(row): self._rate_history_row(row_data))
                self.history_list.setCellWidget(table_row, 5, rate_btn)
            else:
                self.history_list.setCellWidget(table_row, 5, None)
        self._history_loaded_count = target_count

    def _rate_history_row(self, row: dict) -> None:
        label = row["title"]
        if row["type"] == "show" and row["season"] is not None and row["episode"] is not None:
            label = f"{label} S{row['season']:02d}E{row['episode']:02d}"
        dialog = RatingDialog(label, self)
        if not dialog.exec() or dialog.skipped:
            return
        try:
            expected_rating = dialog.rating.value()
            self.services.library.set_rating(
                RatingInput(
                    title_type=row["type"],
                    trakt_id=row["title_trakt_id"],
                    rating=expected_rating,
                    season=row["season"],
                    episode=row["episode"],
                ),
                title=row["title"],
            )
            saved_rating = self.services.library.displayed_history_rating(
                title_type=row["type"],
                trakt_id=row["title_trakt_id"],
                season=row["season"],
                episode=row["episode"],
            )
            if saved_rating != expected_rating:
                raise RuntimeError("Rating did not appear in history after save")
        except Exception as exc:
            QMessageBox.critical(self, "Rating failed", str(exc))
            return
        self._refresh_history()
        QMessageBox.information(self, "Saved", "Rating saved.")

    def _on_history_filter_changed(self) -> None:
        self._refresh_history()

    def _populate_history_title_filter(self, title_type: str | None) -> None:
        current_text = self.history_title_filter.currentText().strip()
        titles = self.services.library.history_titles(title_type=title_type)
        options = [self._ALL_HISTORY_TITLES, *titles]
        existing = [self.history_title_filter.itemText(index) for index in range(self.history_title_filter.count())]
        if existing == options:
            return
        self.history_title_filter.blockSignals(True)
        self.history_title_filter.clear()
        self.history_title_filter.addItems(options)
        if current_text and current_text in titles:
            self.history_title_filter.setCurrentText(current_text)
        else:
            self.history_title_filter.setCurrentText(self._ALL_HISTORY_TITLES)
        self.history_title_filter.blockSignals(False)

    def _on_history_sort_changed(self, column: int, order: Qt.SortOrder) -> None:
        self._history_sort_column = column
        self._history_sort_order = order
        self._refresh_history()

    def _maybe_fetch_more_history_rows(self) -> None:
        scroll = self.history_list.verticalScrollBar()
        if scroll.maximum() > 0 and scroll.value() >= scroll.maximum() - 200:
            if self._is_default_history_sort():
                title_type = self.history_type.currentText()
                if title_type == "all":
                    title_type = None
                title_filter = self.history_title_filter.currentText().strip()
                normalized_title_filter = title_filter if title_filter and title_filter != self._ALL_HISTORY_TITLES else None
                self._load_next_history_page(title_type, normalized_title_filter, reset=False)
            else:
                self._append_history_rows(self._history_loaded_count + self._history_batch_size)

    def _is_default_history_sort(self) -> bool:
        return self._history_sort_column == 0 and self._history_sort_order == Qt.SortOrder.DescendingOrder

    def _load_next_history_page(self, title_type: str | None, title_filter: str | None, reset: bool) -> None:
        if not reset and not self._history_has_more:
            return
        offset = 0 if reset else self._history_loaded_count
        rows = self.services.library.history(
            title_type=title_type,
            title_filter=title_filter,
            limit=self._history_batch_size,
            offset=offset,
        )
        if reset:
            self._history_rows_cache = list(rows)
            self._history_loaded_count = 0
            self.history_list.setRowCount(0)
        elif rows:
            self._history_rows_cache.extend(rows)
        self._history_has_more = len(rows) == self._history_batch_size
        self._append_history_rows(len(self._history_rows_cache))

    def _sort_history_rows(self, rows: list[dict]) -> list[dict]:
        reverse = self._history_sort_order == Qt.SortOrder.DescendingOrder
        return sorted(rows, key=lambda row: self._history_sort_key(row, self._history_sort_column), reverse=reverse)

    def _sort_history_sort_key_text(self, value: str | None) -> tuple[int, str]:
        normalized = (value or "").strip()
        return (0, normalized.casefold()) if normalized else (1, "")

    def _sort_history_sort_key_number(self, value: int | float | None) -> tuple[int, float]:
        return (0, float(value)) if value is not None else (1, float("-inf"))

    def _history_sort_key(self, row: dict, column: int):
        if column == 0:
            return row["watched_at"].timestamp()
        if column == 1:
            return self._sort_history_sort_key_text(row["title"])
        if column == 2:
            return self._sort_history_sort_key_number(row["season"])
        if column == 3:
            return self._sort_history_sort_key_number(row["episode"])
        if column == 4:
            return self._sort_history_sort_key_text(row.get("episode_title"))
        if column == 5:
            return self._sort_history_sort_key_number(row.get("display_rating"))
        if column == 6:
            return self._sort_history_sort_key_number(row.get("display_imdb_rating"))
        return 0

    def _sync_and_refresh_history(self) -> None:
        self._debug_toast("History sync: checking Trakt updates…")
        try:
            self.services.sync.refresh_history()
        except Exception as exc:
            QMessageBox.critical(self, "History sync failed", str(exc))
            self._debug_toast(f"History sync failed: {exc}")
            return
        self._refresh_history()
        self._debug_toast("History sync completed.")

    def _maybe_auto_sync_history(self) -> None:
        if self._history_sync_worker is not None and self._history_sync_worker.isRunning():
            return
        self._debug_toast("History auto-sync: checking…")
        self._history_sync_worker = HistoryAutoSyncWorker(self.services)
        self._history_sync_worker.sync_completed.connect(self._on_history_auto_sync_completed)
        self._history_sync_worker.sync_failed.connect(self._on_history_auto_sync_failed)
        self._history_sync_worker.start()

    def _on_history_auto_sync_completed(self, changed: bool) -> None:
        self._history_sync_worker = None
        if changed:
            self._refresh_history()
            self._debug_toast("History auto-sync updated rows.")
            return
        self._debug_toast("History auto-sync: no changes.")

    def _on_history_auto_sync_failed(self, _message: str) -> None:
        self._history_sync_worker = None
        self._debug_toast("History auto-sync failed.")

    def _sync_imdb_dataset(self, force: bool = False) -> None:
        if self._imdb_sync_worker is not None and self._imdb_sync_worker.isRunning():
            return
        self.imdb_status_label.setText("syncing...")
        self._imdb_sync_worker = IMDbDatasetSyncWorker(self.services, force=force)
        self._imdb_sync_worker.status_changed.connect(self._on_imdb_sync_status_changed)
        self._imdb_sync_worker.sync_completed.connect(self._on_imdb_sync_completed)
        self._imdb_sync_worker.sync_failed.connect(self._on_imdb_sync_failed)
        self._imdb_sync_worker.start()

    def _on_imdb_sync_status_changed(self, message: str) -> None:
        self.imdb_status_label.setText(message)

    def _on_imdb_sync_completed(self, _changed: bool) -> None:
        self.imdb_status_label.setText(self.services.sync.imdb_dataset_status())
        self._refresh_history()
        if self.search_model.total_count() > 0:
            self._start_search_enrichment(list(self.search_model._all_results), self._search_generation)

    def _on_imdb_sync_failed(self, message: str) -> None:
        self.imdb_status_label.setText(self.services.sync.imdb_dataset_status())
        QMessageBox.critical(self, "IMDb sync failed", message)

    def _refresh_upcoming(self) -> None:
        self.upcoming_list.clear()
        unseen_episode_ids = self.services.notifications.unseen_episode_ids()
        for row in self.services.notifications.upcoming_items():
            aired = _format_app_datetime(row["first_aired"], self.services.auth.config.utc_offset) if row["first_aired"] else "unknown"
            prefix = "NEW | " if row.get("episode_trakt_id") in unseen_episode_ids else ""
            self.upcoming_list.addItem(
                f"{prefix}{aired} | {row['show_title']} | S{row['season']:02d}E{row['episode']:02d} {row['episode_title']}"
            )

    def _poll_notifications(self) -> None:
        try:
            items = self.services.notifications.poll_upcoming()
            if items:
                QApplication.beep()
                QApplication.alert(self, 4000)
                if self._tray_icon.isVisible():
                    summary_lines = [f"{item['show_title']} — {item['message']}" for item in items[:3]]
                    if len(items) > 3:
                        summary_lines.append(f"+{len(items) - 3} more")
                    self._tray_icon.showMessage(
                        "New episodes",
                        "\n".join(summary_lines),
                        QSystemTrayIcon.MessageIcon.Information,
                        12000,
                    )
            self._refresh_upcoming()
        except Exception:
            return

    def _start_timer(self) -> None:
        self._timer.stop()
        interval_ms = self.services.auth.config.poll_interval_minutes * 60 * 1000
        self._timer.start(interval_ms)

    def refresh_all(self) -> None:
        self._refresh_history()
        self._mark_startup("history refreshed")
        self._refresh_progress()
        self._mark_startup("progress refreshed")
        self._refresh_upcoming()
        self._mark_startup("upcoming refreshed")
        self._poll_notifications()
        self._mark_startup("notifications polled")
        if self.services.sync.imdb_dataset_status() == "not synced":
            self._sync_imdb_dataset(force=False)
            self._mark_startup("imdb sync scheduled")
        self._finish_startup_profile()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._schedule_window_geometry_save()
        self._update_play_prompt_stack_geometry()
        self._update_debug_toast_stack_geometry()
        if self._progress_items:
            self._refresh_progress()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._schedule_window_geometry_save()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._persist_window_geometry()
        super().closeEvent(event)

    def _schedule_window_geometry_save(self) -> None:
        self._window_state_timer.start(400)

    def _persist_window_geometry(self) -> None:
        config = self.services.auth.config
        config.window_maximized = self.isMaximized()
        if not self.isMaximized():
            config.window_width = max(900, self.width())
            config.window_height = max(600, self.height())
            pos = self.pos()
            config.window_x = pos.x()
            config.window_y = pos.y()
        ConfigStore().save(config)

    def _debug_toast(self, message: str) -> None:
        if not self.services.auth.config.debug_mode or not message:
            return
        while self._debug_toast_stack_layout.count() >= 4:
            item = self._debug_toast_stack_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        toast = DebugToastCard(message, parent=self._debug_toast_stack)
        self._debug_toast_stack_layout.addWidget(toast)
        self._debug_toast_stack.show()
        self._debug_toast_stack.adjustSize()
        self._update_debug_toast_stack_geometry()
        QTimer.singleShot(3600, lambda t=toast: self._dismiss_debug_toast(t))

    def _dismiss_debug_toast(self, toast: QWidget) -> None:
        self._debug_toast_stack_layout.removeWidget(toast)
        toast.deleteLater()
        if self._debug_toast_stack_layout.count() == 0:
            self._debug_toast_stack.hide()
        else:
            self._debug_toast_stack.adjustSize()
        self._update_debug_toast_stack_geometry()

    def _update_debug_toast_stack_geometry(self) -> None:
        if not hasattr(self, "_debug_toast_stack"):
            return
        margin = _scale_px(18)
        width = min(_scale_px(360), max(_scale_px(280), self.width() - margin * 2))
        height = min(max(_scale_px(60), self._debug_toast_stack.sizeHint().height()), max(_scale_px(80), self.height() - margin * 2))
        self._debug_toast_stack.setGeometry(
            max(margin, self.width() - width - margin),
            margin,
            width,
            height,
        )
        self._debug_toast_stack.raise_()

    def _mark_startup(self, name: str) -> None:
        if self._startup_profiler is None or self._startup_finished:
            return
        self._startup_profiler.mark(name)

    def _finish_startup_profile(self) -> None:
        if self._startup_profiler is None or self._startup_finished:
            return
        self._startup_profiler.finish()
        self._startup_finished = True
