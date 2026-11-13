# SPDX-FileCopyrightText: 2026 雾月星辰 & MLXC <github@XingChenRS>
# SPDX-License-Identifier: AGPL-3.0-only
"""Local LK, SakiSU and Vivo Dyn image tasks."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..theme import UiRole
from ..widgets import PolishedComboBox, action_button, path_row
from .common import OFFICIAL_KMIS, page_layout


class ImagesRecoveryPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        page, layout = page_layout(
            "镜像工具", "在本机准备镜像和备份，不直接写入设备"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(page)

        self.task_tabs = QFrame()
        self.task_tabs.setObjectName("taskTabs")
        tabs = QHBoxLayout(self.task_tabs)
        tabs.setContentsMargins(0, 0, 0, 0)
        tabs.setSpacing(3)
        self.task_tab_group = QButtonGroup(self)
        self.task_tab_group.setExclusive(True)
        self.task_tab_buttons: list[QPushButton] = []
        for index, text in enumerate(("SakiSU 修补", "Vivo Dyn", "LK 工具")):
            button = QPushButton(text)
            button.setObjectName("taskTab")
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, target=index: self._set_task(target)
            )
            self.task_tab_group.addButton(button, index)
            self.task_tab_buttons.append(button)
            tabs.addWidget(button)
        self.task_tab_buttons[0].setChecked(False)
        self.task_tab_buttons[0].setVisible(False)
        layout.addWidget(self.task_tabs)

        self.task_stack = QStackedWidget()
        self.task_stack.setObjectName("taskStack")
        self.task_stack.addWidget(self._sakisu_panel())
        self.task_stack.addWidget(self._dyn_panel())
        self.task_stack.addWidget(self._lk_panel())
        layout.addWidget(self.task_stack)
        layout.addStretch(1)
        self._set_task(2)
        self.apply_capabilities(frozenset())

    def _intro(self, title: str, summary: str) -> QFrame:
        intro = QFrame()
        intro.setObjectName("taskIntro")
        box = QVBoxLayout(intro)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("sectionHeading")
        description = QLabel(summary)
        description.setObjectName("sectionDescription")
        description.setWordWrap(True)
        box.addWidget(heading)
        box.addWidget(description)
        return intro

    def _sakisu_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("imageTaskPanel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(15, 13, 15, 14)
        box.setSpacing(11)
        box.addWidget(
            self._intro(
                "生成 SakiSU 修补镜像",
                "选择原始 boot 或 init_boot，确认 KMI；这里只生成文件。",
            )
        )
        self.sakisu_source_edit = QLineEdit()
        self.sakisu_source_edit.setReadOnly(True)
        self.sakisu_source_browse = QPushButton("选择")
        self.sakisu_partition_combo = PolishedComboBox()
        self.sakisu_partition_combo.addItems(("boot", "init_boot"))
        self.sakisu_kmi_combo = PolishedComboBox()
        self.sakisu_kmi_combo.addItems(OFFICIAL_KMIS)
        fields = QGridLayout()
        fields.setHorizontalSpacing(10)
        fields.setVerticalSpacing(5)
        for column, text in enumerate(("原始镜像", "镜像类型", "内核 KMI")):
            caption = QLabel(text)
            caption.setObjectName("fieldCaption")
            fields.addWidget(caption, 0, column)
        fields.addWidget(path_row(self.sakisu_source_edit, self.sakisu_source_browse), 1, 0)
        fields.addWidget(self.sakisu_partition_combo, 1, 1)
        fields.addWidget(self.sakisu_kmi_combo, 1, 2)
        fields.setColumnStretch(0, 2)
        fields.setColumnStretch(1, 1)
        fields.setColumnStretch(2, 1)
        box.addLayout(fields)

        self.sakisu_candidate_edit = QLineEdit()
        self.sakisu_candidate_edit.setVisible(False)
        self.sakisu_output_label = QLabel("选择原始镜像后显示输出文件")
        self.sakisu_output_label.setObjectName("outputSummary")
        self.sakisu_candidate_edit.textChanged.connect(
            lambda value: self.sakisu_output_label.setText(
                value or "选择原始镜像后显示输出文件"
            )
        )
        self.sakisu_patch_button = action_button(
            "生成修补镜像", "sakisuPatchOnlyAction", role=UiRole.PRIMARY
        )
        output = QHBoxLayout()
        output.addWidget(self.sakisu_output_label, 1)
        output.addWidget(self.sakisu_patch_button)
        box.addLayout(output)

        self.sakisu_wrong_kmi_confirm = QCheckBox("我已核对 KMI")
        self.sakisu_replace_non_stock_confirm = QCheckBox("来源不是原厂镜像")
        advanced = QFrame()
        advanced.setObjectName("advancedOptions")
        advanced_box = QHBoxLayout(advanced)
        advanced_box.setContentsMargins(10, 7, 10, 7)
        advanced_box.addWidget(self.sakisu_wrong_kmi_confirm)
        advanced_box.addWidget(self.sakisu_replace_non_stock_confirm)
        advanced_box.addStretch(1)
        box.addWidget(advanced)

        self.sakisu_rollback_edit = QLineEdit()
        self.sakisu_receipt_edit = QLineEdit()
        for carrier in (
            self.sakisu_candidate_edit,
            self.sakisu_rollback_edit,
            self.sakisu_receipt_edit,
        ):
            carrier.setVisible(False)
            box.addWidget(carrier)
        return panel

    def _dyn_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("imageTaskPanel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(15, 13, 15, 14)
        box.setSpacing(11)
        box.addWidget(
            self._intro(
                "Vivo Dyn 备份",
                "从当前设备导出 Dyn，或恢复由本工具生成的备份。",
            )
        )
        self.dyn_backup_edit = QLineEdit()
        self.dyn_backup_edit.setReadOnly(True)
        self.dyn_backup_browse = QPushButton("选择备份")
        box.addWidget(path_row(self.dyn_backup_edit, self.dyn_backup_browse))
        actions = QHBoxLayout()
        self.dyn_capture_button = action_button(
            "导出 Dyn 备份", "vivoDynCaptureAction", role=UiRole.PRIMARY
        )
        self.dyn_restore_button = action_button(
            "恢复所选 Dyn", "vivoDynRestoreAction", role=UiRole.DANGER
        )
        actions.addWidget(self.dyn_capture_button)
        actions.addWidget(self.dyn_restore_button)
        actions.addStretch(1)
        box.addLayout(actions)

        box.addLayout(actions)
        return panel

    def _lk_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("imageTaskPanel")
        box = QVBoxLayout(panel)
        box.setContentsMargins(15, 13, 15, 14)
        box.setSpacing(11)
        box.addWidget(
            self._intro(
                "LK 镜像工具",
                "检查或生成本地 LK 镜像；写入操作位于“Root 与解锁”。",
            )
        )
        self.source_lk_edit = QLineEdit()
        self.source_lk_browse = QPushButton("选择 LK")
        self.output_lk_edit = QLineEdit()
        self.output_lk_browse = QPushButton("输出位置")
        box.addWidget(path_row(self.source_lk_edit, self.source_lk_browse))
        self.output_lk_row = path_row(self.output_lk_edit, self.output_lk_browse)
        self.output_lk_row.setVisible(False)
        box.addWidget(self.output_lk_row)
        self.local_lk_profile_combo = PolishedComboBox()
        self.local_lk_profile_combo.setObjectName("localLkProfileCombo")
        self.local_lk_profile_combo.addItem("自动识别", "")
        self.local_lk_profile_combo.addItem("MT6985 及以下", "mt6985_or_lower")
        self.local_lk_profile_combo.setToolTip(
            "仅选择本地 LK 镜像结构，不绑定已连接设备或机型模板"
        )
        box.addWidget(self.local_lk_profile_combo)
        actions = QHBoxLayout()
        self.inspect_lk_button = action_button(
            "选择并检查 LK", "imageInspectLkAction", role=UiRole.PRIMARY
        )
        self.patch_lk_button = action_button(
            "生成修补 LK", "imagePatchLkAction", role=UiRole.SECONDARY
        )
        actions.addWidget(self.inspect_lk_button)
        actions.addWidget(self.patch_lk_button)
        actions.addStretch(1)
        box.addLayout(actions)
        self.image_status_label = QLabel("尚未运行镜像任务。")
        self.image_status_label.setObjectName("inlineStatus")
        box.addWidget(self.image_status_label)
        return panel

    def _set_task(self, index: int) -> None:
        self.task_stack.setCurrentIndex(index)
        self.task_tab_buttons[index].setChecked(True)

    def apply_capabilities(
        self,
        capabilities: frozenset[str],
        *,
        test_mode: bool = False,
        mode: str | None = None,
    ) -> None:
        self.patch_lk_button.setEnabled(True)
        self.inspect_lk_button.setEnabled(True)
        normalized_mode = str(mode or "").strip().casefold()
        dyn_visible = (
            (not normalized_mode or normalized_mode == "adb")
            and ("xingc" in capabilities or test_mode)
        )
        self.task_tab_buttons[1].setVisible(dyn_visible)
        if not dyn_visible and self.task_stack.currentIndex() == 1:
            self._set_task(2)
# provenance: NBMTK-canary-7f3a91c4e8b2d05f
