"""A GeoLens server connection: URL, QGIS auth config, capabilities and transport."""

from __future__ import annotations

from io import BytesIO
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsAuthMethodConfig,
    QgsBlockingNetworkRequest,
    QgsFeedback,
)
from qgis.PyQt.QtCore import QByteArray, QCoreApplication, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from .client import (
    Dataset,
    GeoLensClient,
    GeoLensError,
    same_origin,
    with_default_scheme,
)

SERVER_SETTING = "geolens/server"
AUTHCFG_SETTING = "geolens/authcfg"
LEGACY_KEY_SETTING = "geolens/api_key"
LEGACY_KEY_SERVER_SETTING = "geolens/api_key_server"
AUTH_HEADER = "X-Api-Key"


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self._body


class QgisTransport:
    """A `urlopen`-compatible opener that sends requests through QGIS networking.

    QGIS proxy, SSL and network timeout settings apply, and `authcfg` names
    the QGIS authentication config whose credentials each request carries.
    HTTP errors raise `HTTPError` and transport failures `OSError`, as
    `urlopen` does, so `GeoLensClient` works the same with either opener.
    """

    def __init__(
        self,
        authcfg: str = "",
        upload_progress: Callable[[int, int], Any] | None = None,
    ):
        self.authcfg = authcfg
        self.upload_progress = upload_progress
        self._feedback: QgsFeedback | None = None

    def abort(self) -> None:
        """Cancel the request in flight. Safe to call from another thread."""
        if self._feedback is not None:
            self._feedback.cancel()

    def __call__(self, request: Request, timeout: float | None = None) -> _Response:
        url = request.full_url
        network_request = QNetworkRequest(QUrl(url))
        for name, value in request.header_items():
            network_request.setRawHeader(
                name.encode("latin-1"), value.encode("latin-1")
            )
        if self.authcfg:
            mismatch = auth_server_mismatch(self.authcfg, url)
            if mismatch:
                raise GeoLensError(mismatch)
            # Applied to this request only, not through setAuthCfg:
            # QgsBlockingNetworkRequest re-applies its auth config to every
            # redirect target, so a redirect to another host would get the key.
            manager = QgsApplication.authManager()
            applied, network_request = manager.updateNetworkRequest(
                network_request, self.authcfg
            )
            if not applied or not _carries_credential(network_request):
                raise GeoLensError(
                    "The selected QGIS authentication configuration can't be "
                    "used with GeoLens. Choose an API Header configuration that "
                    "sends X-Api-Key."
                )
        credential = _carries_credential(network_request)
        blocking = QgsBlockingNetworkRequest()
        if self.upload_progress is not None:
            blocking.uploadProgress.connect(self.upload_progress)
        feedback = self._feedback = QgsFeedback()
        method = request.get_method()
        payload = QByteArray(request.data or b"")
        if method == "GET":
            error = blocking.get(
                network_request,
                True,
                feedback,
                QgsBlockingNetworkRequest.RequestFlag.EmptyResponseIsValid,
            )
        elif method == "POST":
            error = blocking.post(network_request, payload, True, feedback)
        elif method == "PUT":
            error = blocking.put(network_request, payload, feedback)
        elif method == "DELETE":
            error = blocking.deleteResource(network_request, feedback)
        else:
            raise ValueError(f"Unsupported HTTP method {method}")
        reply = blocking.reply()
        # The redirected request went out without the credential, so its
        # answer is what an anonymous caller sees.
        final = urlparse(reply.request().url().toString())
        if credential and final.hostname and reply.request().url() != QUrl(url):
            raise GeoLensError(
                f"GeoLens redirected the request to {final.scheme}://"
                f"{final.hostname}. Update the server URL to match."
            )
        body = bytes(reply.content())
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if isinstance(status, int) and status >= 400:
            raise HTTPError(url, status, blocking.errorMessage(), {}, BytesIO(body))
        if error != QgsBlockingNetworkRequest.ErrorCode.NoError:
            raise OSError(blocking.errorMessage())
        return _Response(body)


def auth_server_mismatch(authcfg: str, url: str, auth_manager=None) -> str:
    """Why `authcfg` must not be sent to `url`, or "" when it may be.

    A config may go only to the scheme, host and port of the server URL it
    was saved with, the URI that `store_api_key` records.
    """
    auth_manager = auth_manager or QgsApplication.authManager()
    loaded, config = auth_manager.loadAuthenticationConfig(
        authcfg, QgsAuthMethodConfig(), False
    )
    saved_for = config.uri().strip() if loaded else ""
    if saved_for and same_origin(saved_for, url):
        return ""
    target = origin_label(url)
    if not loaded:
        return QCoreApplication.translate(
            "GeoLens",
            "The selected authentication configuration no longer exists in the "
            "QGIS authentication database, so GeoLens won't use it for {target}. "
            "Choose another configuration.",
        ).format(target=target)
    if saved_for:
        return QCoreApplication.translate(
            "GeoLens",
            "The selected authentication configuration was saved for {saved}, "
            "so GeoLens won't send it to {target}. Choose a configuration saved "
            "for this server, or paste its API key.",
        ).format(saved=origin_label(saved_for), target=target)
    return QCoreApplication.translate(
        "GeoLens",
        "The selected authentication configuration doesn't name a server, so "
        "GeoLens won't send it to {target}. Set its URL to this GeoLens server "
        "in the QGIS authentication settings, or choose another configuration.",
    ).format(target=target)


def origin_label(url: str) -> str:
    """`url` as scheme://host[:port] for messages, or as typed when it won't parse."""
    try:
        parsed = urlparse(with_default_scheme(url))
    except ValueError:
        return url.strip()
    host = parsed.netloc.rpartition("@")[2]
    return f"{parsed.scheme.lower()}://{host.lower()}" if host else url.strip()


def _carries_credential(request: QNetworkRequest) -> bool:
    if bytes(request.rawHeader(AUTH_HEADER.encode())).strip():
        return True
    return bytes(request.rawHeader(b"Authorization")).startswith(b"Bearer ")


class GeoLensConnection:
    """The server the dock talks to, the credentials it uses, and what it supports.

    A pasted `api_key` is held in memory for the session; otherwise `authcfg`
    supplies the credentials. `opener` replaces the QGIS transport in tests.
    """

    def __init__(
        self,
        server_url: str,
        authcfg: str = "",
        api_key: str = "",
        opener: Callable | None = None,
    ):
        self.authcfg = "" if api_key.strip() else authcfg
        self.transport = opener or QgisTransport(self.authcfg)
        self.client = GeoLensClient(server_url, api_key, self.transport)
        self.capabilities: dict[str, Any] = {}

    @property
    def base_url(self) -> str:
        return self.client.base_url

    def connect(self) -> list[Dataset]:
        """Fetch the first page of the catalog and the server's capabilities."""
        datasets = self.client.search(limit=50)
        self.capabilities = self.client.capabilities()
        return datasets


def store_api_key(
    key: str, server_url: str, notify, intro: str, auth_manager=None
) -> str:
    """Save `key` as an APIHeader auth config sending `X-Api-Key`.

    `notify(text, level)` gets `intro` first, with a warning when QGIS is about
    to ask for its master password. Returns the new config id, or "" when QGIS
    could not store it, including when the user cancels that prompt.
    """
    auth_manager = auth_manager or QgsApplication.authManager()
    if not auth_manager.masterPasswordIsSet():
        intro += " QGIS will ask for its master password."
    notify(intro, Qgis.MessageLevel.Info)
    config = QgsAuthMethodConfig("APIHeader")
    config.setName(f"GeoLens API key ({urlparse(server_url).netloc or server_url})")
    config.setUri(server_url)
    config.setConfig(AUTH_HEADER, key)
    stored, config = auth_manager.storeAuthenticationConfig(config)
    if not stored or not config.id():
        return ""
    # Callers delete their only other copy of the key on success, so check
    # that the database really holds it.
    loaded, saved = auth_manager.loadAuthenticationConfig(
        config.id(), QgsAuthMethodConfig(), True
    )
    if not loaded or saved.config(AUTH_HEADER) != key:
        auth_manager.removeAuthenticationConfig(config.id())
        return ""
    return config.id()


def migrate_plaintext_key(settings, notify, auth_manager=None) -> str:
    """Move a key saved in plain text by older versions into the QGIS auth database.

    On success the plain text setting is deleted, the new config becomes the
    selected one unless the user already selected another, and its id is
    returned. On failure the plain text
    key is kept so a later attempt can move it, and "" is returned. A key
    saved without a server URL is kept too, since a config without one is
    never sent.
    `notify(text, level)` reports progress in the message bar.
    """
    key = str(settings.value(LEGACY_KEY_SETTING, "") or "").strip()
    if not key:
        forget_legacy_key(settings)
        return ""
    server_url = legacy_key_server(settings)
    if not server_url:
        return ""
    authcfg = store_api_key(
        key,
        server_url,
        notify,
        "Moving your saved GeoLens API key into the QGIS authentication database.",
        auth_manager,
    )
    if not authcfg:
        notify(
            "Your GeoLens API key could not be moved into the QGIS "
            "authentication database, so it stays in the QGIS profile settings "
            "for now. GeoLens will try again in your next QGIS session.",
            Qgis.MessageLevel.Warning,
        )
        return ""
    if not settings.value(AUTHCFG_SETTING, ""):
        settings.setValue(AUTHCFG_SETTING, authcfg)
    forget_legacy_key(settings)
    settings.sync()
    notify(
        "Your GeoLens API key is now in the QGIS authentication database, and "
        "the plain-text copy was removed.",
        Qgis.MessageLevel.Success,
    )
    return authcfg


def legacy_key_server(settings) -> str:
    """The server a plain-text key from an older version belongs to, or "".

    Older versions saved the key next to the last server URL. Connecting
    elsewhere overwrites that URL, so the first read records it for the key.
    """
    if not settings.contains(LEGACY_KEY_SERVER_SETTING):
        settings.setValue(
            LEGACY_KEY_SERVER_SETTING,
            str(settings.value(SERVER_SETTING, "") or "").strip(),
        )
    return str(settings.value(LEGACY_KEY_SERVER_SETTING, "") or "")


def forget_legacy_key(settings) -> None:
    """Delete a plain-text key from an older version and the server it belongs to."""
    settings.remove(LEGACY_KEY_SETTING)
    settings.remove(LEGACY_KEY_SERVER_SETTING)
