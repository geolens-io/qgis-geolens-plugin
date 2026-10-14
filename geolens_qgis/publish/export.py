"""Export QGIS layers to the files GeoLens ingests.

``plan_export`` runs on the GUI thread and sorts layers into files to write,
layers that already live on the target server, and layers GeoLens can't take.
``ExportTask`` writes the files in the background.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsTask,
    QgsVectorFileWriter,
    QgsVectorLayerFeatureSource,
)

VECTOR = "vector"
TABLE = "table"
RASTER = "raster"

# Raster providers that draw pre-rendered tiles, which are not source data.
_SERVICE_RASTER_PROVIDERS = {"wms", "wmts", "arcgismapserver"}
_GEOTIFF_SUFFIXES = {".tif", ".tiff"}
# Leaves room for the extension and a uniqueness counter in a 255 byte name.
_MAX_STEM_BYTES = 200
_UNSAFE_FILE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_WINDOWS_DEVICE_NAMES = re.compile(r"(?i)(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])")


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    layer_id: str
    layer_name: str
    message: str


@dataclass(frozen=True)
class Reference:
    """A layer loaded from the target server, published as a dataset link."""

    layer_id: str
    layer_name: str
    dataset_id: str
    editable: bool


@dataclass(frozen=True)
class PlannedLayer:
    layer: object
    kind: str
    selected_only: bool = False


@dataclass
class ExportPlan:
    to_export: list[PlannedLayer] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


@dataclass(frozen=True)
class ExportedLayer:
    layer_id: str
    layer_name: str
    kind: str
    path: Path
    size: int
    sha256: str
    # False when the layer's own GeoTIFF is uploaded as it is.
    temporary: bool


@dataclass
class ExportResult:
    exported: list[ExportedLayer] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


class ExportError(RuntimeError):
    pass


def _server_key(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def plan_export(
    layers: Iterable,
    server_url: str,
    selected_only: Iterable[str] = (),
) -> ExportPlan:
    """Sort `layers` into references, skips and files to export.

    `selected_only` holds the ids of vector layers to export with only their
    selected features.
    """
    plan = ExportPlan()
    selected_ids = set(selected_only)
    target = _server_key(server_url)
    for layer in layers:
        layer_id, name = layer.id(), layer.name()
        dataset_id = layer.customProperty("geolens/dataset_id")
        loaded_from = layer.customProperty("geolens/server")
        # A selection-only request must export those features, not the dataset.
        if (
            dataset_id
            and loaded_from
            and layer_id not in selected_ids
            and _server_key(str(loaded_from)) == target
        ):
            plan.references.append(
                Reference(
                    layer_id,
                    name,
                    str(dataset_id),
                    layer.customProperty("geolens/editable") is True,
                )
            )
            plan.issues.append(
                Issue(
                    "A034",
                    "info",
                    layer_id,
                    name,
                    "Loaded from this GeoLens, so the map references its dataset "
                    "and nothing is uploaded.",
                )
            )
            continue
        planned = _classify(layer, layer_id in selected_ids, plan.issues)
        if planned:
            plan.to_export.append(planned)
    return plan


def _classify(layer, selected_only: bool, issues: list[Issue]) -> PlannedLayer | None:
    layer_id, name = layer.id(), layer.name()
    layer_type = layer.type()
    if not layer.isValid():
        issues.append(
            Issue(
                "A030",
                "warning",
                layer_id,
                name,
                "This layer's data source is unavailable, so it will be skipped.",
            )
        )
        return None
    if layer_type == Qgis.LayerType.Vector:
        if _has_no_features(layer):
            issues.append(_empty(layer_id, name, "Layer is empty and will be skipped."))
            return None
        if selected_only and layer.selectedFeatureCount() == 0:
            issues.append(
                _empty(
                    layer_id,
                    name,
                    "No features are selected, so the layer will be skipped.",
                )
            )
            return None
        kind = VECTOR if layer.isSpatial() else TABLE
        if kind == TABLE:
            issues.append(
                Issue(
                    "A032",
                    "info",
                    layer_id,
                    name,
                    "Table without geometry: published as a table, "
                    "not shown on the map.",
                )
            )
        return PlannedLayer(layer, kind, selected_only)
    if (
        layer_type == Qgis.LayerType.Raster
        and layer.providerType() not in _SERVICE_RASTER_PROVIDERS
    ):
        if _geotiff_path(layer) is None:
            issues.append(
                Issue(
                    "A030",
                    "warning",
                    layer_id,
                    name,
                    "This raster format can't be published yet, so it will be "
                    "skipped. Convert it to a GeoTIFF to publish it.",
                )
            )
            return None
        return PlannedLayer(layer, RASTER)
    issues.append(
        Issue(
            "A030",
            "warning",
            layer_id,
            name,
            "This layer type can't be published and will be skipped.",
        )
    )
    return None


def _has_no_features(layer) -> bool:
    count = layer.featureCount()
    if count < 0:
        # Unknown and uncounted both mean the provider must be read to know.
        request = QgsFeatureRequest().setLimit(1).setNoAttributes()
        return next(layer.getFeatures(request), None) is None
    return count == 0


def _empty(layer_id: str, name: str, message: str) -> Issue:
    return Issue("A031", "warning", layer_id, name, message)


def safe_file_stem(name: str) -> str:
    stem = _UNSAFE_FILE_CHARS.sub("", name).strip().strip(".")
    # Windows treats these as devices whatever follows the first dot.
    if _WINDOWS_DEVICE_NAMES.fullmatch(stem.split(".", 1)[0].rstrip()):
        stem = f"_{stem}"
    stem = stem.encode("utf-8")[:_MAX_STEM_BYTES].decode("utf-8", errors="ignore")
    return stem.strip().strip(".") or "layer"


def _unique_path(folder: Path, stem: str, suffix: str) -> Path:
    path = folder / f"{stem}{suffix}"
    counter = 2
    while path.exists():
        path = folder / f"{stem}_{counter}{suffix}"
        counter += 1
    return path


def sha256_of(path: Path, is_canceled=lambda: False) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            if is_canceled():
                raise ExportError("Export canceled.")
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class _Job:
    """Everything the worker thread needs, copied on the GUI thread.

    QGIS layers belong to the GUI thread, so `ExportTask.run` works from
    feature sources, cloned raster providers and plain values instead.
    """

    layer_id: str
    name: str
    kind: str
    existing_geotiff: Path | None = None
    source: object = None
    fields: object = None
    wkb_type: object = None
    crs: object = None
    fids: list[int] | None = None


def _snapshot(planned: PlannedLayer) -> _Job:
    layer = planned.layer
    job = _Job(layer.id(), layer.name(), planned.kind)
    if planned.kind == RASTER:
        job.existing_geotiff = _geotiff_path(layer)
        return job
    job.source = QgsVectorLayerFeatureSource(layer)
    job.fields, job.wkb_type, job.crs = layer.fields(), layer.wkbType(), layer.crs()
    if planned.selected_only:
        job.fids = list(layer.selectedFeatureIds())
    return job


def _free_name(fields, name: str) -> str:
    taken = {field.name().lower() for field in fields}
    while name.lower() in taken:
        name = f"_{name}"
    return name


def _write_vector(job: _Job, path: Path, transform_context, is_canceled) -> None:
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG" if job.kind == TABLE else "FlatGeobuf"
    options.fileEncoding = "UTF-8"
    if job.kind == VECTOR:
        # FlatGeobuf refuses features with no geometry while it builds an index.
        options.layerOptions = ["SPATIAL_INDEX=NO"]
    elif job.kind == TABLE:
        # The driver would adopt a source field called "fid" as its primary key.
        options.layerOptions = [f"FID={_free_name(job.fields, '_geolens_fid')}"]
    # A GeoPackage names its table; FlatGeobuf takes the file name. SQLite
    # keeps the sqlite_ prefix for itself.
    options.layerName = (
        f"_{path.stem}" if path.stem.lower().startswith("sqlite_") else path.stem
    )
    writer = QgsVectorFileWriter.create(
        str(path), job.fields, job.wkb_type, job.crs, transform_context, options
    )
    if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
        raise ExportError(f"Could not export {job.name}: {writer.errorMessage()}")
    request = QgsFeatureRequest()
    if job.fids is not None:
        request.setFilterFids(job.fids)
    canceled = False
    for feature in job.source.getFeatures(request):
        if is_canceled():
            canceled = True
            break
        if not writer.addFeature(feature):
            raise ExportError(f"Could not export {job.name}: {writer.errorMessage()}")
    # Deleting the writer flushes and closes the file.
    del writer
    if canceled:
        raise ExportError("Export canceled.")


def _geotiff_path(layer) -> Path | None:
    """The layer's own GeoTIFF, when that file alone describes the raster.

    GDAL lists every file a dataset depends on, so a TIFF that also needs a
    world file, PAM or other sidecar can't be uploaded bare. Neither can one
    whose layer CRS was overridden in QGIS, since the file would still carry
    the old projection.
    """
    from osgeo import gdal

    if layer.providerType() != "gdal":
        return None
    path = Path(layer.source().split("|", 1)[0])
    if path.suffix.lower() not in _GEOTIFF_SUFFIXES or not path.is_file():
        return None
    if layer.crs() != layer.dataProvider().crs():
        return None
    dataset = gdal.Open(str(path))
    if dataset is None:
        return None
    files = dataset.GetFileList()
    dataset = None
    return path if len(files) == 1 else None


class ExportTask(QgsTask):
    """Writes the planned layers to `folder`.

    The constructor copies what the worker needs from the layers, so call it
    on the GUI thread; later edits to a layer don't change the export. The
    layers stay locked for the task's lifetime, so a QGIS user can't remove
    one mid-export. Read `result` once the task finishes; if `run` returned
    False, `error` says why.
    """

    def __init__(
        self,
        plan: ExportPlan,
        folder: str | Path | None,
        transform_context,
        max_file_size_bytes: int | None = None,
        description: str = "Export layers for GeoLens",
    ):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.folder = Path(folder or tempfile.mkdtemp(prefix="geolens_publish_"))
        self.transform_context = transform_context
        self.max_file_size_bytes = max_file_size_bytes
        self.result = ExportResult(
            references=list(plan.references), issues=list(plan.issues)
        )
        self.error = ""
        self._jobs = [_snapshot(planned) for planned in plan.to_export]
        self.setDependentLayers([planned.layer for planned in plan.to_export])

    def run(self) -> bool:
        total = max(1, len(self._jobs))
        try:
            for index, job in enumerate(self._jobs):
                if self.isCanceled():
                    return False
                self.result.exported.append(self._export(job))
                self.setProgress(100 * (index + 1) / total)
        except (ExportError, OSError) as error:
            self.error = str(error)
            return False
        return True

    def _export(self, job: _Job) -> ExportedLayer:
        path = job.existing_geotiff
        if path is None:
            suffix = ".fgb" if job.kind == VECTOR else ".gpkg"
            path = _unique_path(self.folder, safe_file_stem(job.name), suffix)
        try:
            if job.existing_geotiff is None:
                _write_vector(job, path, self.transform_context, self.isCanceled)
            size = path.stat().st_size
            checksum = sha256_of(path, self.isCanceled)
        except (ExportError, OSError):
            # Never leave a truncated file where a finished one is expected.
            if job.existing_geotiff is None:
                path.unlink(missing_ok=True)
            raise
        if self.max_file_size_bytes is not None and size > self.max_file_size_bytes:
            self.result.issues.append(
                Issue(
                    "A010",
                    "error",
                    job.layer_id,
                    job.name,
                    f"The exported file is {size:,} bytes, over the server's "
                    f"limit of {self.max_file_size_bytes:,}.",
                )
            )
        return ExportedLayer(
            job.layer_id,
            job.name,
            job.kind,
            path,
            size,
            checksum,
            job.existing_geotiff is None,
        )
