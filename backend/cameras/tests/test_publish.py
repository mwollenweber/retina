import json
from pathlib import Path

import pytest

from cameras.publish import publish, publish_inline


@pytest.mark.django_db
def test_publish_inline_writes_local_file(settings, vetted_camera, approved_photo, tmp_path):
    settings.RETINA["DATA_BUCKET"] = None
    settings.RETINA["LOCAL_GEOJSON_PATH"] = str(tmp_path / "out" / "cameras.geojson")
    where = publish_inline()
    assert "cameras.geojson" in where
    data = json.loads(Path(settings.RETINA["LOCAL_GEOJSON_PATH"]).read_text())
    assert data["count"] == 1
    assert data["features"][0]["properties"]["photos"][0]["url"].endswith(".jpg")


@pytest.mark.django_db
def test_publish_invokes_lambda_when_configured(settings, monkeypatch):
    settings.RETINA["PUBLISH_FUNCTION_NAME"] = "retina-publish"
    calls = {}

    class FakeLambda:
        def invoke(self, **kw):
            calls.update(kw)

    import boto3

    monkeypatch.setattr(boto3, "client", lambda name: FakeLambda())
    assert publish() == "invoked retina-publish"
    assert calls["FunctionName"] == "retina-publish"
