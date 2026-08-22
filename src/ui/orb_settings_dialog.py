"""Dialog for user-adjustable ORB scoring and validity settings."""

from __future__ import annotations

from typing import Mapping

from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from src.risk.orb_position import DEFAULT_ORB_SETTINGS, OrbSettings


class OrbSettingsDialog(QDialog):
    """Edit ORB lower bounds, scoring ideals, and upper bounds."""

    _ROWS = (
        (
            "Capital allocation (%)",
            "capital_min_percent",
            "capital_ideal_percent",
            "capital_max_percent",
            100.0,
        ),
        (
            "Stop / ADR (%)",
            "stop_adr_min_percent",
            "stop_adr_ideal_percent",
            "stop_adr_max_percent",
            1000.0,
        ),
    )

    def __init__(
        self,
        values: Mapping[str, float] | OrbSettings | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("ORB Settings")
        self.setMinimumWidth(620)
        self.spins: dict[str, QDoubleSpinBox] = {}

        layout = QVBoxLayout(self)
        description = QLabel(
            "Bounds determine whether an ORB position plan is valid. "
            "Ideal values determine how valid plans are scored and ranked."
        )
        description.setWordWrap(True)
        layout.addWidget(description)

        grid = QGridLayout()
        grid.addWidget(QLabel("Metric"), 0, 0)
        grid.addWidget(QLabel("Lower bound"), 0, 1)
        grid.addWidget(QLabel("Ideal value"), 0, 2)
        grid.addWidget(QLabel("Upper bound"), 0, 3)
        for row, (label, lower, ideal, upper, maximum) in enumerate(
            self._ROWS, start=1
        ):
            grid.addWidget(QLabel(label), row, 0)
            for column, name in enumerate((lower, ideal, upper), start=1):
                spin = QDoubleSpinBox()
                spin.setObjectName(f"{name}_spin")
                spin.setRange(0.0, maximum)
                spin.setDecimals(2)
                spin.setSingleStep(0.5)
                spin.setSuffix(" %")
                grid.addWidget(spin, row, column)
                self.spins[name] = spin
        layout.addLayout(grid)

        note = QLabel(
            "Capital allocation must be at least the lower bound and below "
            "the upper bound. Stop / ADR may equal either bound."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.RestoreDefaults
            | QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            self.reset_defaults
        )
        layout.addWidget(buttons)

        self._set_values(OrbSettings.from_mapping(values))

    def _set_values(self, settings: OrbSettings) -> None:
        for name, value in settings.to_dict().items():
            self.spins[name].setValue(value)

    def orb_settings(self) -> OrbSettings:
        return OrbSettings(
            **{name: spin.value() for name, spin in self.spins.items()}
        )

    def values(self) -> dict[str, float]:
        return self.orb_settings().to_dict()

    def reset_defaults(self) -> None:
        self._set_values(DEFAULT_ORB_SETTINGS)

    def _accept_if_valid(self) -> None:
        try:
            self.orb_settings()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid ORB Settings", str(exc))
            return
        self.accept()
