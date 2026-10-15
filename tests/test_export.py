"""Layer export against a real QGIS."""

import hashlib
import itertools

import pytest

pytest.importorskip("pytest_qgis")

from qgis.core import (  # noqa: E402
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsApplication,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
    QgsVectorTileLayer,
)


from geolens_qgis.publish.export import (  # noqa: E402
    ExportTask,
    plan_export,
    safe_file_stem,
)

SERVER = "https://geolens.example.com"


@pytest.fixture(autouse=True)
def _qgis(real_qgis, qgis_app):
    return qgis_app


def memory_layer(geometry, name, rows=3, fields="field=name:string"):
    layer = QgsVectorLayer(f"{geometry}?crs=epsg:4326&{fields}", name, "memory")
    features = []
    for index in range(rows):
        feature = QgsFeature(layer.fields())
        feature.setAttributes([f"row {index}"])
        if geometry == "Point":
            feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(index, index)))
        elif geometry == "LineString":
            feature.setGeometry(
                QgsGeometry.fromPolylineXY([QgsPointXY(index, 0), QgsPointXY(index, 1)])
            )
        elif geometry == "Polygon":
            feature.setGeometry(
                QgsGeometry.fromPolygonXY(
                    [
                        [
                            QgsPointXY(index, 0),
                            QgsPointXY(index + 1, 0),
                            QgsPointXY(index + 1, 1),
                            QgsPointXY(index, 0),
                        ]
                    ]
                )
            )
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    return layer


def geotiff(path):
    from osgeo import gdal, osr

    gdal.UseExceptions()
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), 8, 8, 1, gdal.GDT_Byte)
    dataset.SetGeoTransform((0, 1, 0, 8, 0, -1))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetProjection(srs.ExportToWkt())
    dataset.GetRasterBand(1).Fill(7)
    dataset.FlushCache()
    return QgsRasterLayer(str(path), path.stem)


def run(layers, tmp_path, server=SERVER, selected_only=(), **kwargs):
    """Run the task on QGIS's own worker threads, as the plugin does."""
    plan = plan_export(layers, server, selected_only)
    (tmp_path / "out").mkdir(exist_ok=True)
    task = ExportTask(plan, tmp_path / "out", QgsCoordinateTransformContext(), **kwargs)
    QgsApplication.taskManager().addTask(task)
    assert task.waitForFinished(30_000)
    assert task.status() == QgsTask.TaskStatus.Complete, task.error
    return task


def feature_count(path):
    return QgsVectorLayer(str(path), "check", "ogr").featureCount()


@pytest.mark.parametrize("geometry", ["Point", "LineString", "Polygon"])
def test_vector_layers_become_flatgeobuf_with_their_checksum(geometry, tmp_path):
    task = run([memory_layer(geometry, geometry.lower())], tmp_path)
    (exported,) = task.result.exported
    assert exported.kind == "vector"
    assert exported.path.suffix == ".fgb"
    assert feature_count(exported.path) == 3
    assert exported.size == exported.path.stat().st_size
    assert exported.sha256 == hashlib.sha256(exported.path.read_bytes()).hexdigest()
    assert exported.temporary


def test_a_spatial_layer_with_a_null_geometry_still_exports(tmp_path):
    layer = memory_layer("Point", "points", rows=2)
    empty = QgsFeature(layer.fields())
    empty.setAttributes(["no geometry"])
    layer.dataProvider().addFeatures([empty])
    (exported,) = run([layer], tmp_path).result.exported
    assert feature_count(exported.path) == 3


def test_a_table_without_geometry_becomes_a_geopackage(tmp_path):
    task = run([memory_layer("NoGeometry", "people")], tmp_path)
    (exported,) = task.result.exported
    assert exported.kind == "table"
    assert exported.path.suffix == ".gpkg"
    assert feature_count(exported.path) == 3
    assert [issue.code for issue in task.result.issues] == ["A032"]


def test_a_table_named_like_a_sqlite_internal_table_still_exports(tmp_path):
    task = run([memory_layer("NoGeometry", "sqlite_results")], tmp_path)
    (exported,) = task.result.exported
    assert feature_count(exported.path) == 3


@pytest.mark.parametrize("name", ["gpkg_contents", "gpkg_spatial_ref_sys"])
def test_a_table_named_like_a_geopackage_metadata_table_still_exports(name, tmp_path):
    task = run([memory_layer("NoGeometry", name)], tmp_path)
    (exported,) = task.result.exported
    assert feature_count(exported.path) == 3


def test_a_table_with_a_text_field_named_fid_still_exports(tmp_path):
    layer = QgsVectorLayer("NoGeometry?field=fid:string", "ids", "memory")
    rows = []
    for value in ("a", "a"):
        feature = QgsFeature(layer.fields())
        feature.setAttributes([value])
        rows.append(feature)
    layer.dataProvider().addFeatures(rows)
    (exported,) = run([layer], tmp_path).result.exported
    check = QgsVectorLayer(str(exported.path), "check", "ogr")
    assert check.featureCount() == 2
    assert [f["fid"] for f in check.getFeatures()] == ["a", "a"]


def test_selected_features_only(tmp_path):
    layer = memory_layer("Point", "points", rows=5)
    layer.selectByIds([1, 2])
    task = run([layer], tmp_path, selected_only=[layer.id()])
    assert feature_count(task.result.exported[0].path) == 2


def test_the_export_is_a_snapshot_of_the_layer_when_the_task_was_made(tmp_path):
    layer = memory_layer("Point", "points", rows=5)
    layer.selectByIds([1, 2])
    plan = plan_export([layer], SERVER, [layer.id()])
    (tmp_path / "out").mkdir()
    task = ExportTask(plan, tmp_path / "out", QgsCoordinateTransformContext())
    layer.selectByIds([1, 2, 3, 4])
    layer.setName("renamed")
    assert task.run(), task.error
    (exported,) = task.result.exported
    assert exported.layer_name == "points"
    assert feature_count(exported.path) == 2


def test_cancelling_during_the_write_fails_the_task_and_leaves_no_file(tmp_path):
    plan = plan_export([memory_layer("Point", "points")], SERVER)
    (tmp_path / "out").mkdir()
    task = ExportTask(plan, tmp_path / "out", QgsCoordinateTransformContext())
    # Not canceled when the task starts a layer, canceled once the writer runs.
    states = itertools.chain([False], itertools.repeat(True))
    task.isCanceled = lambda: next(states)
    assert task.run() is False
    assert task.result.exported == []
    assert list((tmp_path / "out").iterdir()) == []


def test_cancelling_while_hashing_an_existing_geotiff_fails_the_task(tmp_path):
    layer = geotiff(tmp_path / "dem.tif")
    plan = plan_export([layer], SERVER)
    (tmp_path / "out").mkdir()
    task = ExportTask(plan, tmp_path / "out", QgsCoordinateTransformContext())
    states = itertools.chain([False], itertools.repeat(True))
    task.isCanceled = lambda: next(states)
    assert task.run() is False
    assert task.result.exported == []
    assert (tmp_path / "dem.tif").exists()


def test_a_filesystem_error_after_writing_removes_the_partial_file(
    tmp_path, monkeypatch
):
    plan = plan_export([memory_layer("Point", "points")], SERVER)
    (tmp_path / "out").mkdir()
    task = ExportTask(plan, tmp_path / "out", QgsCoordinateTransformContext())

    def unreadable(*_):
        raise OSError("unreadable")

    monkeypatch.setattr("geolens_qgis.publish.export.sha256_of", unreadable)
    assert task.run() is False
    assert task.error == "unreadable"
    assert list((tmp_path / "out").iterdir()) == []


def test_layer_with_no_selection_is_skipped_when_selected_only(tmp_path):
    layer = memory_layer("Point", "points")
    plan = plan_export([layer], SERVER, [layer.id()])
    assert plan.to_export == []
    assert [issue.code for issue in plan.issues] == ["A031"]


def test_the_selection_is_checked_before_the_provider_is_counted(tmp_path):
    layer = memory_layer("Point", "points")

    def slow_count():
        raise AssertionError("counted the provider")

    layer.featureCount = slow_count
    plan = plan_export([layer], SERVER, [layer.id()])
    assert plan.to_export == []
    assert [issue.code for issue in plan.issues] == ["A031"]


def test_a_raster_that_stops_qualifying_before_the_task_starts_is_skipped(tmp_path):
    layer = geotiff(tmp_path / "dem.tif")
    plan = plan_export([layer], SERVER)
    assert len(plan.to_export) == 1
    layer.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    (tmp_path / "out").mkdir()
    task = ExportTask(plan, tmp_path / "out", QgsCoordinateTransformContext())
    assert task.run(), task.error
    assert task.result.exported == []
    assert [issue.code for issue in task.result.issues] == ["A030"]
    assert list((tmp_path / "out").iterdir()) == []


class _WriterProxy:
    """Wraps a real writer to make its flush or its writes misbehave."""

    def __init__(self, writer, flushes=True, drop_from=None):
        self._writer, self._flushes, self._drop_from = writer, flushes, drop_from
        self._added = 0

    def addFeature(self, feature):
        self._added += 1
        if self._drop_from is not None and self._added >= self._drop_from:
            return True
        return self._writer.addFeature(feature)

    def flushBuffer(self):
        self._writer.flushBuffer()
        return self._flushes

    def __getattr__(self, name):
        return getattr(self._writer, name)


def break_writer(monkeypatch, **options):
    from geolens_qgis.publish import export

    create = export.QgsVectorFileWriter.create
    monkeypatch.setattr(
        export,
        "QgsVectorFileWriter",
        type(
            "Writer",
            (),
            {
                "SaveVectorOptions": export.QgsVectorFileWriter.SaveVectorOptions,
                "WriterError": export.QgsVectorFileWriter.WriterError,
                "create": staticmethod(
                    lambda *args: _WriterProxy(create(*args), **options)
                ),
            },
        ),
    )


def make_task(tmp_path):
    plan = plan_export([memory_layer("Point", "points")], SERVER)
    (tmp_path / "out").mkdir()
    return ExportTask(plan, tmp_path / "out", QgsCoordinateTransformContext())


def test_a_failed_flush_fails_the_export_and_leaves_no_file(tmp_path, monkeypatch):
    break_writer(monkeypatch, flushes=False)
    task = make_task(tmp_path)
    assert task.run() is False
    assert task.result.exported == []
    assert list((tmp_path / "out").iterdir()) == []


def test_a_file_that_closes_short_fails_the_export_and_leaves_no_file(
    tmp_path, monkeypatch
):
    break_writer(monkeypatch, drop_from=2)
    task = make_task(tmp_path)
    assert task.run() is False
    assert "not written fully" in task.error
    assert list((tmp_path / "out").iterdir()) == []


def test_subset_string_limits_the_export(tmp_path):
    layer = memory_layer("Point", "points", rows=5)
    assert layer.setSubsetString("\"name\" = 'row 3'")
    task = run([layer], tmp_path)
    assert feature_count(task.result.exported[0].path) == 1


def test_geotiff_is_uploaded_as_it_is(tmp_path):
    layer = geotiff(tmp_path / "dem.tif")
    task = run([layer], tmp_path)
    (exported,) = task.result.exported
    assert exported.kind == "raster"
    assert exported.path == tmp_path / "dem.tif"
    assert not exported.temporary
    assert exported.sha256 == hashlib.sha256(exported.path.read_bytes()).hexdigest()


def skipped_with_a030(layer):
    plan = plan_export([layer], SERVER)
    assert plan.to_export == []
    assert [(i.code, i.severity) for i in plan.issues] == [("A030", "warning")]
    assert "GeoTIFF" in plan.issues[0].message


def test_a_geotiff_with_a_pam_sidecar_is_skipped(tmp_path):
    from osgeo import gdal

    layer = geotiff(tmp_path / "dem.tif")
    dataset = gdal.Open(str(tmp_path / "dem.tif"))
    dataset.GetRasterBand(1).ComputeStatistics(False)
    dataset = None
    assert (tmp_path / "dem.tif.aux.xml").exists()
    skipped_with_a030(layer)


def test_a_geotiff_georeferenced_by_a_world_file_is_skipped(tmp_path):
    from osgeo import gdal

    plain = gdal.GetDriverByName("GTiff").Create(
        str(tmp_path / "scan.tif"), 8, 8, 1, gdal.GDT_Byte
    )
    plain = None
    (tmp_path / "scan.tfw").write_text("1\n0\n0\n-1\n0\n8\n")
    skipped_with_a030(QgsRasterLayer(str(tmp_path / "scan.tif"), "scan"))


def test_a_geotiff_whose_layer_crs_was_overridden_is_skipped(tmp_path):
    layer = geotiff(tmp_path / "dem.tif")
    layer.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    skipped_with_a030(layer)


def test_other_raster_formats_are_skipped(tmp_path):
    from osgeo import gdal

    source = tmp_path / "grid.tif"
    geotiff(source)
    gdal.Translate(str(tmp_path / "grid.asc"), str(source), format="AAIGrid")
    layers = [
        QgsRasterLayer(str(tmp_path / "grid.asc"), "grid"),
        memory_layer("Point", "points"),
    ]
    plan = plan_export(layers, SERVER)
    assert [planned.layer.name() for planned in plan.to_export] == ["points"]
    assert [(i.code, i.layer_name) for i in plan.issues] == [("A030", "grid")]


def test_layers_are_locked_for_the_task(tmp_path):
    layer = memory_layer("Point", "points")
    plan = plan_export([layer], SERVER)
    task = ExportTask(plan, tmp_path, QgsCoordinateTransformContext())
    assert [locked.id() for locked in task.dependentLayers()] == [layer.id()]


def test_a_file_over_the_server_limit_raises_a010(tmp_path):
    task = run([memory_layer("Point", "points")], tmp_path, max_file_size_bytes=10)
    assert [(i.code, i.severity) for i in task.result.issues] == [("A010", "error")]


def test_empty_layers_are_skipped(tmp_path):
    layer = memory_layer("Point", "empty", rows=0)
    plan = plan_export([layer], SERVER)
    assert plan.to_export == []
    assert [issue.code for issue in plan.issues] == ["A031"]


@pytest.mark.parametrize("state", ["UnknownCount", "Uncounted"])
@pytest.mark.parametrize("rows, skipped", [(0, True), (2, False)])
def test_layers_with_an_unknown_feature_count_are_probed(
    state, rows, skipped, monkeypatch
):
    layer = memory_layer("Point", "lazy", rows=rows)
    monkeypatch.setattr(
        layer, "featureCount", lambda: getattr(Qgis.FeatureCountState, state)
    )
    plan = plan_export([layer], SERVER)
    assert (plan.to_export == []) is skipped


def test_layer_types_geolens_cannot_take_are_reported(tmp_path):
    layers = [
        QgsVectorTileLayer("type=xyz&url=https://example.com/{z}/{x}/{y}.pbf", "vt"),
        QgsRasterLayer(
            "type=xyz&url=https://example.com/{z}/{x}/{y}.png", "xyz", "wms"
        ),
    ]
    plan = plan_export(layers, SERVER)
    assert plan.to_export == []
    assert [(i.code, i.layer_name) for i in plan.issues] == [
        ("A030", "vt"),
        ("A030", "xyz"),
    ]


def test_layers_whose_source_is_unavailable_are_skipped_not_fatal(tmp_path):
    broken = [
        QgsRasterLayer(str(tmp_path / "missing.tif"), "gone"),
        QgsVectorLayer(str(tmp_path / "missing.gpkg"), "lost", "ogr"),
    ]
    good = memory_layer("Point", "points")
    task = run([*broken, good], tmp_path)
    assert [item.layer_name for item in task.result.exported] == ["points"]
    assert [(i.code, i.layer_name) for i in task.result.issues] == [
        ("A030", "gone"),
        ("A030", "lost"),
    ]


def tagged(layer, server, dataset_id="abc"):
    layer.setCustomProperty("geolens/server", server)
    layer.setCustomProperty("geolens/dataset_id", dataset_id)
    return layer


def test_a_layer_from_the_target_server_becomes_a_reference(tmp_path):
    layer = tagged(memory_layer("Point", "points"), SERVER + "/")
    task = run([layer], tmp_path)
    assert task.result.exported == []
    (reference,) = task.result.references
    assert (reference.layer_id, reference.dataset_id) == (layer.id(), "abc")
    assert [issue.code for issue in task.result.issues] == ["A034"]


def test_a_selected_only_layer_from_the_target_server_is_exported(tmp_path):
    layer = tagged(memory_layer("Point", "points", rows=4), SERVER)
    layer.selectByIds([1, 2])
    task = run([layer], tmp_path, selected_only=[layer.id()])
    assert task.result.references == []
    assert feature_count(task.result.exported[0].path) == 2


def test_a_layer_from_another_geolens_is_exported(tmp_path):
    layer = tagged(memory_layer("Point", "points"), "https://other.example.org")
    task = run([layer], tmp_path)
    assert task.result.references == []
    assert len(task.result.exported) == 1


def test_file_names_drop_control_characters_and_stay_unique(tmp_path):
    assert safe_file_stem("a\x00b/c\nd") == "abcd"
    assert safe_file_stem("..") == "layer"
    assert safe_file_stem("CON") == "_CON"
    assert safe_file_stem("com1.parks") == "_com1.parks"
    assert safe_file_stem("console") == "console"
    assert safe_file_stem("COM\u00b2") == "_COM\u00b2"
    long = safe_file_stem("é" * 300)
    assert len(long.encode("utf-8")) <= 200
    layers = [memory_layer("Point", "same"), memory_layer("Point", "same")]
    task = run(layers, tmp_path)
    assert sorted(item.path.name for item in task.result.exported) == [
        "same.fgb",
        "same_2.fgb",
    ]
