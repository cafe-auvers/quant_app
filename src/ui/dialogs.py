from __future__ import annotations

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout, QPushButton,
    QLineEdit, QKeySequenceEdit, QMessageBox, QLabel, QTreeWidget,
    QTreeWidgetItem, QAbstractItemView, QHeaderView,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QKeySequence

from src.services.app_state import SETTINGS_FILE
from src.ui.filter_catalog import DEFAULT_SETTINGS, FILTER_CATALOG
from src.utils.storage import load_json, save_json

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(400)
        self.main_window = parent
        
        self.settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
        if "shortcuts" not in self.settings:
            self.settings["shortcuts"] = DEFAULT_SETTINGS["shortcuts"].copy()
        else:
            for k, v in DEFAULT_SETTINGS["shortcuts"].items():
                if k not in self.settings["shortcuts"]:
                    self.settings["shortcuts"][k] = v

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        
        form_group = QGroupBox("Keyboard Shortcuts")
        form_layout = QFormLayout()
        
        self.shortcut_edits = {}
        
        shortcut_labels = {
            "set_target": "Set Breakout Price",
            "draw_line": "Draw Line",
            "erase_drawing": "Erase Drawing",
            "full_view": "Full View / Reset",
            "add_watchlist": "Add to Watchlist",
            "prev_symbol": "Previous Watchlist Symbol",
            "next_symbol": "Next Watchlist Symbol",
            "pan_left": "Pan Left",
            "pan_right": "Pan Right"
        }
        
        shortcuts = self.settings["shortcuts"]
        for key, label in shortcut_labels.items():
            key_seq = QKeySequence(shortcuts.get(key, ""))
            edit_widget = QKeySequenceEdit(key_seq)
            form_layout.addRow(label, edit_widget)
            self.shortcut_edits[key] = edit_widget
            
        form_group.setLayout(form_layout)
        layout.addWidget(form_group)
        
        btn_layout = QHBoxLayout()
        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.setObjectName("resetDefaultsButton")
        reset_btn.clicked.connect(self.reset_defaults)
        
        save_btn = QPushButton("Save")
        save_btn.setObjectName("saveSettingsButton")
        save_btn.clicked.connect(self.save_settings)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancelButton")
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(reset_btn)
        btn_layout.addStretch(1)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        
        layout.addLayout(btn_layout)
        self.setLayout(layout)

    def reset_defaults(self):
        for key, val in DEFAULT_SETTINGS["shortcuts"].items():
            if key in self.shortcut_edits:
                self.shortcut_edits[key].setKeySequence(QKeySequence(val))

    def save_settings(self):
        new_shortcuts = {}
        for key, edit in self.shortcut_edits.items():
            seq_str = edit.keySequence().toString()
            new_shortcuts[key] = seq_str
            
        from collections import Counter
        seq_counts = Counter(v for v in new_shortcuts.values() if v)
        conflicts = [seq for seq, count in seq_counts.items() if count > 2]
        if conflicts:
            QMessageBox.warning(
                self,
                "Shortcut Conflict",
                f"The key sequence '{conflicts[0]}' is assigned to {seq_counts[conflicts[0]]} functions.\n"
                "A single key combination cannot be assigned to more than 2 functions at the same time."
            )
            return

        self.settings["shortcuts"] = new_shortcuts
        save_json(SETTINGS_FILE, self.settings)


class AddFilterDialog(QDialog):
    def __init__(self, parent=None, disabled_attributes=None):
        super().__init__(parent)
        self.setWindowTitle("Select Filter Metric")
        self.setMinimumSize(980, 680)
        self.selected_attribute = None
        self.disabled_attributes = disabled_attributes or set()
        
        # Remove the "?" question mark next to the X button
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        
        # Premium, balanced stylesheet (Clean sizes: headers larger, tree items 12px, cancel/select 13px)
        self.setStyleSheet("""
            QDialog {
                background-color: #ffffff;
            }
            QLabel {
                color: #5d606b;
                font-size: 14px;
                font-weight: 500;
            }
            QLineEdit {
                border: 1px solid #d1d4dc;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 14px;
                color: #131722;
                background-color: #ffffff;
            }
            QLineEdit:focus {
                border: 1px solid #2962ff;
            }
            QTreeWidget {
                border: 1px solid #e0e3eb;
                border-radius: 6px;
                background-color: #ffffff;
                color: #131722;
                font-size: 14px;
            }
            QTreeWidget::item {
                padding: 8px;
                border-bottom: 1px solid #f0f3f6;
            }
            QTreeWidget::item:selected {
                background-color: #e2e4ea;
                color: #131722;
                font-weight: bold;
            }
            QHeaderView::section {
                background-color: #f8f9fa;
                color: #131722;
                font-weight: bold;
                padding: 10px;
                border: none;
                border-bottom: 2px solid #e0e3eb;
                font-size: 14px;
            }
            QPushButton {
                background-color: #2962ff;
                color: white;
                font-weight: bold;
                border-radius: 6px;
                padding: 10px 24px;
                font-size: 14px;
                border: none;
            }
            QPushButton:hover {
                background-color: #1a56db;
            }
            QPushButton:disabled {
                background-color: #e0e3eb;
                color: #b2b5be;
            }
            QPushButton#cancelBtn {
                background-color: #f0f3f6;
                color: #131722;
                border: 1px solid #d1d4dc;
            }
            QPushButton#cancelBtn:hover {
                background-color: #e0e3eb;
            }
        """)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        
        header_lbl = QLabel("Double-click a metric or select and click 'Select Filter' to add it to your rules.")
        layout.addWidget(header_lbl)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("ðŸ”Ž Search filters by name, category, or description...")
        self.search_input.textChanged.connect(self.filter_tree)
        layout.addWidget(self.search_input)
        
        # QTreeWidget
        from PyQt5.QtWidgets import QTreeWidget, QTreeWidgetItem
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Filter Name", "Explanation", "Suggested Value or Range"])
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.itemDoubleClicked.connect(self.accept_selection)
        self.tree.itemSelectionChanged.connect(self.on_selection_changed)
        
        # Enable column resizing and set generous initial column widths
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(2, QHeaderView.Interactive)
        
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(1, 460)
        self.tree.setColumnWidth(2, 220)
        
        layout.addWidget(self.tree)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.clicked.connect(self.reject)
        
        self.select_btn = QPushButton("Select Filter")
        self.select_btn.clicked.connect(self.accept_selection)
        self.select_btn.setEnabled(False)
        
        button_layout.addWidget(self.cancel_btn)
        button_layout.addWidget(self.select_btn)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
        self.populate_tree()
        
    def populate_tree(self):
        from PyQt5.QtWidgets import QTreeWidgetItem
        
        # Group metrics by category
        categories = {}
        for cat, key, name, expl, sugg in FILTER_CATALOG:
            if cat not in categories:
                categories[cat] = []
            categories[cat].append((key, name, expl, sugg))
            
        self.tree.clear()
        
        for cat_name, items in categories.items():
            cat_item = QTreeWidgetItem(self.tree)
            cat_item.setText(0, cat_name)
            cat_item.setFirstColumnSpanned(True)
            cat_item.setFlags(cat_item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsDragEnabled)
            cat_item.setExpanded(True)
            
            # Category Header node styling - Bold, visually distinct background
            font = cat_item.font(0)
            font.setBold(True)
            cat_item.setFont(0, font)
            cat_item.setBackground(0, QColor("#eef1f6"))
            cat_item.setForeground(0, QColor("#131722"))
            
            for key, name, expl, sugg in items:
                child_item = QTreeWidgetItem(cat_item)
                is_disabled = key in self.disabled_attributes
                display_name = f"✓ {name}" if is_disabled else name
                
                child_item.setText(0, display_name)
                child_item.setData(0, Qt.UserRole, key)
                child_item.setText(1, expl)
                child_item.setText(2, sugg)
                
                for col in range(3):
                    if is_disabled:
                        child_item.setForeground(col, QColor("#b2b5be"))
                    else:
                        child_item.setForeground(col, QColor("#131722"))
                        
    def filter_tree(self, query):
        query = query.lower().strip()
        if not query:
            # Show all categories and children
            for i in range(self.tree.topLevelItemCount()):
                cat_item = self.tree.topLevelItem(i)
                cat_item.setHidden(False)
                for j in range(cat_item.childCount()):
                    cat_item.child(j).setHidden(False)
            return
            
        for i in range(self.tree.topLevelItemCount()):
            cat_item = self.tree.topLevelItem(i)
            any_child_visible = False
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                match = False
                for col in range(3):
                    if query in child.text(col).lower():
                        match = True
                        break
                child.setHidden(not match)
                if match:
                    any_child_visible = True
                    
            cat_item.setHidden(not any_child_visible)
            if any_child_visible:
                cat_item.setExpanded(True)
                
    def on_selection_changed(self):
        selected = self.tree.selectedItems()
        if not selected:
            self.select_btn.setEnabled(False)
            return
        item = selected[0]
        if item.parent() is None:
            self.select_btn.setEnabled(False)
            return
        key = item.data(0, Qt.UserRole)
        is_disabled = key in self.disabled_attributes
        self.select_btn.setEnabled(not is_disabled)
        
    def accept_selection(self):
        selected = self.tree.selectedItems()
        if not selected:
            return
        item = selected[0]
        if item.parent() is None:
            return
        key = item.data(0, Qt.UserRole)
        if key in self.disabled_attributes:
            return
        self.selected_attribute = key
        self.accept()


