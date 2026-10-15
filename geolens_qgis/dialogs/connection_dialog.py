"""A dialog that creates or edits one saved GeoLens connection."""

from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)
from qgis.gui import QgsAuthConfigSelect

from ..connection import auth_server_mismatch, origin_label, store_api_key
from ..connections import SavedConnection, normalized_url, same_name, valid_name
from ..i18n import tr


class ConnectionDialog(QDialog):
    """Name, server URL and credentials for one connection.

    `taken` lists the names of the other saved connections. `notify(text,
    level)` reports progress while a pasted key is saved. After `exec()`
    accepts, `connection()` is what the user saved.
    """

    def __init__(self, parent, taken, connection=None, notify=None):
        super().__init__(parent)
        self.setWindowTitle(
            tr("Edit GeoLens Connection")
            if connection
            else tr("New GeoLens Connection")
        )
        self._taken = list(taken)
        self._notify = notify or (lambda *_: None)
        self._saved: SavedConnection | None = None
        # The auth config last deselected for another server, and the warning
        # an OK click right after that edit shows before saving without it.
        self._dropped_authcfg = ""
        self._pending_warning = ""

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(connection.name if connection else "")
        self.name_edit.setToolTip(tr("A name for this connection in the GeoLens panel"))
        self.name_edit.setAccessibleName(tr("Connection name"))
        self.url_edit = QLineEdit(connection.url if connection else "")
        self.url_edit.setPlaceholderText("https://demo.getgeolens.com")
        self.url_edit.setToolTip(tr("The address of the GeoLens server"))
        self.url_edit.setAccessibleName(tr("Server URL"))
        self.url_edit.editingFinished.connect(self._server_edited)
        self.auth_select = QgsAuthConfigSelect(self)
        self.auth_select.setToolTip(
            tr(
                "A QGIS authentication configuration that sends X-Api-Key, "
                "saved for this server. Leave it empty for anonymous access."
            )
        )
        self.auth_select.setAccessibleName(tr("Authentication configuration"))
        if connection and connection.authcfg:
            self.auth_select.setConfigId(connection.authcfg)
        self.auth_select.selectedConfigIdChanged.connect(self._auth_selection_changed)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(tr("Optional; replaces the configuration"))
        self.key_edit.setToolTip(
            tr(
                "Paste a GeoLens API key to save it in the QGIS authentication "
                "database for this server. It is encrypted with the QGIS master "
                "password."
            )
        )
        self.key_edit.setAccessibleName(tr("API key to save"))
        form.addRow(tr("Name"), self.name_edit)
        form.addRow(tr("Server URL"), self.url_edit)
        form.addRow(tr("Authentication"), self.auth_select)
        form.addRow(tr("API key"), self.key_edit)
        layout.addLayout(form)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setAccessibleName(tr("Connection dialog message"))
        self.notice.hide()
        layout.addWidget(self.notice)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def connection(self) -> SavedConnection | None:
        return self._saved

    def _show(self, text):
        self.notice.setText(text)
        self.notice.setVisible(bool(text))

    def _auth_selection_changed(self, *_):
        # Any pick, including clearing it, supersedes a deselection.
        self._dropped_authcfg = ""
        self._pending_warning = ""

    def _server_edited(self):
        url = self.url_edit.text()
        self._pending_warning = ""
        if not url.strip():
            return
        authcfg = self.auth_select.configId()
        mismatch = auth_server_mismatch(authcfg, url) if authcfg else ""
        if mismatch:
            self.auth_select.setConfigId("")
            self._dropped_authcfg = authcfg
            self._pending_warning = (
                mismatch + " " + tr("The configuration is no longer selected.")
            )
            self._show(self._pending_warning)
            return
        dropped = self._dropped_authcfg
        if dropped and not authcfg and not auth_server_mismatch(dropped, url):
            self.auth_select.setConfigId(dropped)
            self._show(
                tr(
                    "The authentication configuration saved for {server} is "
                    "selected again."
                ).format(server=origin_label(url))
            )

    def accept(self):
        name = self.name_edit.text().strip()
        if not valid_name(name):
            self._show(tr("Enter a name for the connection, without / or \\."))
            return
        if any(same_name(name, other) for other in self._taken):
            self._show(
                tr("A connection named {name} already exists.").format(name=name)
            )
            return
        try:
            url = normalized_url(self.url_edit.text())
        except ValueError:
            self._show(tr("Enter the server's HTTP(S) URL."))
            return
        # Leaving the Server URL field to click OK can deselect the
        # configuration just before the click lands, so refuse that click once.
        warning, self._pending_warning = self._pending_warning, ""
        if warning:
            self._show(
                warning + " " + tr("Click OK again to save the connection without it.")
            )
            return
        authcfg = self.auth_select.configId()
        key = self.key_edit.text().strip()
        if key:
            authcfg = store_api_key(
                key,
                url,
                self._notify,
                tr("Saving the API key in the QGIS authentication database."),
            )
            if not authcfg:
                self._show(
                    tr(
                        "The API key could not be saved in the QGIS "
                        "authentication database. Cancel and paste it in the "
                        "GeoLens panel to use it for this session only."
                    )
                )
                return
            self.key_edit.clear()
        elif authcfg:
            mismatch = auth_server_mismatch(authcfg, url)
            if mismatch:
                self._show(mismatch)
                return
        self._saved = SavedConnection(name, url, authcfg)
        super().accept()
