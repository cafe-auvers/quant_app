"""Separate production-health tab and audit journal viewer."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import List

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.services.event_journal import load_recent_events
from src.services.health import (
    HealthContext,
    HealthLevel,
    HealthSnapshot,
    collect_health_snapshot,
)
from src.services.order_ledger import load_order_ledger


class HealthProbeWorker(QThread):
    completed = pyqtSignal(object, list)
    failed = pyqtSignal(str)

    def __init__(self, context: HealthContext, journal_symbol: str = "") -> None:
        super().__init__()
        self.context = context
        self.journal_symbol = journal_symbol

    def run(self) -> None:
        try:
            try:
                context = replace(
                    self.context,
                    orders=tuple(load_order_ledger()),
                    order_ledger_error="",
                )
            except Exception as exc:
                context = replace(self.context, order_ledger_error=str(exc))
            snapshot = collect_health_snapshot(context)
            events = load_recent_events(limit=300, symbol=self.journal_symbol)
            self.completed.emit(snapshot, events)
        except Exception as exc:
            self.failed.emit(str(exc))


class HealthPanelMixin:
    """Build and refresh a read-only system-health view."""

    _HEALTH_COLORS = {
        HealthLevel.HEALTHY: QColor("#d1fae5"),
        HealthLevel.WARNING: QColor("#fef3c7"),
        HealthLevel.CRITICAL: QColor("#fee2e2"),
        HealthLevel.UNKNOWN: QColor("#e5e7eb"),
    }

    def _build_health_tab(self) -> None:
        layout = QVBoxLayout(self.health_widget)

        controls = QHBoxLayout()
        title = QLabel("Production Health")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        controls.addWidget(title)
        controls.addStretch()
        self.health_checked_at_label = QLabel("Not checked yet")
        controls.addWidget(self.health_checked_at_label)
        self.health_refresh_button = QPushButton("Refresh Health")
        self.health_refresh_button.clicked.connect(self.refresh_health_panel)
        controls.addWidget(self.health_refresh_button)
        layout.addLayout(controls)

        self.health_summary_label = QLabel(
            "Open this tab or click Refresh Health to run read-only local checks."
        )
        self.health_summary_label.setWordWrap(True)
        layout.addWidget(self.health_summary_label)

        health_group = QGroupBox("System checks")
        health_layout = QVBoxLayout(health_group)
        self.health_checks_table = QTableWidget(0, 4)
        self.health_checks_table.setHorizontalHeaderLabels(
            ["Component", "Status", "Summary", "Detail"]
        )
        self.health_checks_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.health_checks_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.health_checks_table.setAlternatingRowColors(True)
        header = self.health_checks_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        health_layout.addWidget(self.health_checks_table)
        layout.addWidget(health_group, 2)

        journal_group = QGroupBox("Trading event journal")
        journal_layout = QVBoxLayout(journal_group)
        journal_controls = QHBoxLayout()
        journal_controls.addWidget(QLabel("Symbol:"))
        self.health_journal_symbol_input = QLineEdit()
        self.health_journal_symbol_input.setPlaceholderText("All symbols")
        self.health_journal_symbol_input.setMaximumWidth(180)
        self.health_journal_symbol_input.returnPressed.connect(
            self.refresh_health_panel
        )
        journal_controls.addWidget(self.health_journal_symbol_input)
        journal_controls.addStretch()
        journal_controls.addWidget(
            QLabel("Newest first; accounts are masked and secrets are redacted.")
        )
        journal_layout.addLayout(journal_controls)

        self.health_journal_table = QTableWidget(0, 7)
        self.health_journal_table.setHorizontalHeaderLabels(
            [
                "Time",
                "Event",
                "Symbol",
                "Strategy",
                "Account",
                "Qty / Price",
                "Reason / Payload",
            ]
        )
        self.health_journal_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.health_journal_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.health_journal_table.setAlternatingRowColors(True)
        journal_header = self.health_journal_table.horizontalHeader()
        for column in range(6):
            journal_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        journal_header.setSectionResizeMode(6, QHeaderView.Stretch)
        journal_layout.addWidget(self.health_journal_table)
        layout.addWidget(journal_group, 3)

    def _health_context(self) -> HealthContext:
        reconciliation_worker = self.__dict__.get("order_reconciliation_worker")
        pc_database_engine = self.__dict__.get("pc_db_engine")
        if pc_database_engine is None:
            pc_database_engine = self.__dict__.get("_pc_probe_engine")
        kis_workers = (
            self.__dict__.get("kis_account_worker"),
            self.__dict__.get("kis_startup_worker"),
        )
        mirror_tickers = list(self.__dict__.get("universe_tickers") or []) or None
        return HealthContext(
            db_source=str(self.__dict__.get("db_engine_source", "none")),
            db_initializing=bool(self.__dict__.get("db_initializing", False)),
            pc_database_ready=bool(self.__dict__.get("_pc_database_ready", False)),
            pc_database_engine=pc_database_engine,
            mirror_engine=self.__dict__.get("_local_mirror_engine"),
            mirror_tickers=mirror_tickers,
            kis_snapshot_count=len(self.__dict__.get("kis_account_snapshots", {})),
            kis_request_running=any(
                worker is not None and worker.isRunning() for worker in kis_workers
            ),
            kis_last_success_at=str(self.__dict__.get("_kis_api_last_success_at", "")),
            kis_last_error=str(self.__dict__.get("_kis_api_last_error", "")),
            orders=tuple(self.__dict__.get("order_ledger", ())),
            reconciliation_running=bool(
                reconciliation_worker is not None and reconciliation_worker.isRunning()
            ),
            reconciliation_last_success_at=str(
                self.__dict__.get("_last_order_reconciliation_at", "")
            ),
            reconciliation_last_error=str(
                self.__dict__.get("_last_order_reconciliation_error", "")
            ),
        )

    def refresh_health_panel(self, *args) -> None:
        worker = self.__dict__.get("_health_probe_worker")
        if worker is not None and worker.isRunning():
            return
        self.health_refresh_button.setEnabled(False)
        self.health_checked_at_label.setText("Checking...")
        symbol = self.health_journal_symbol_input.text().strip().upper()
        worker = HealthProbeWorker(self._health_context(), symbol)
        self._health_probe_worker = worker
        worker.completed.connect(self._on_health_probe_completed)
        worker.failed.connect(self._on_health_probe_failed)
        self._track_worker("_health_probe_worker", worker)
        worker.start()

    def _on_health_probe_completed(
        self, snapshot: HealthSnapshot, events: List[dict]
    ) -> None:
        self.health_refresh_button.setEnabled(True)
        self.health_checked_at_label.setText(f"Checked {snapshot.checked_at}")
        counts = {
            level: sum(check.level == level for check in snapshot.checks)
            for level in HealthLevel
        }
        self.health_summary_label.setText(
            f"Overall: {snapshot.overall_level.value} - "
            f"{counts[HealthLevel.HEALTHY]} healthy, "
            f"{counts[HealthLevel.WARNING]} warning, "
            f"{counts[HealthLevel.CRITICAL]} critical, "
            f"{counts[HealthLevel.UNKNOWN]} unknown."
        )
        self.health_checks_table.setRowCount(len(snapshot.checks))
        for row, check in enumerate(snapshot.checks):
            values = [
                check.component,
                check.level.value,
                check.summary,
                check.detail,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                if column == 1:
                    item.setBackground(self._HEALTH_COLORS[check.level])
                    item.setTextAlignment(Qt.AlignCenter)
                self.health_checks_table.setItem(row, column, item)
        self._populate_health_journal(events)

    def _on_health_probe_failed(self, message: str) -> None:
        self.health_refresh_button.setEnabled(True)
        self.health_checked_at_label.setText("Health check failed")
        self.health_summary_label.setText(
            f"The read-only health probe failed: {message}"
        )

    def _populate_health_journal(self, events: List[dict]) -> None:
        self.health_journal_table.setRowCount(len(events))
        for row, event in enumerate(events):
            quantity = event.get("quantity")
            price = event.get("price")
            qty_price = " / ".join(
                part
                for part in (
                    f"{quantity} sh" if quantity is not None else "",
                    f"{price}" if price is not None else "",
                )
                if part
            )
            detail = str(event.get("reason") or "")
            payload = event.get("payload")
            if payload:
                payload_text = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(", ", ": ")
                )
                detail = f"{detail} | {payload_text}" if detail else payload_text
            values = [
                event.get("timestamp", ""),
                event.get("event_type", ""),
                event.get("symbol", ""),
                event.get("strategy_id", ""),
                event.get("account", ""),
                qty_price,
                detail,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setToolTip(str(value or ""))
                self.health_journal_table.setItem(row, column, item)
