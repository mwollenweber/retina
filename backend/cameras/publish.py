"""
Publish the vetted-camera GeoJSON to S3 (or a local file for dev) and
invalidate CloudFront. The scheduled Lambda does the same thing every 15
minutes; this path exists so an admin can publish immediately after review.
"""

import json
import logging
from pathlib import Path

from django.conf import settings

from .geojson import build_feature_collection

log = logging.getLogger(__name__)


def publish_inline() -> str:
    """Build and write the GeoJSON. Returns a description of where it went."""
    cfg = settings.RETINA
    body = json.dumps(build_feature_collection(), separators=(",", ":")).encode()

    bucket = cfg.get("DATA_BUCKET")
    if not bucket:
        path = Path(cfg["LOCAL_GEOJSON_PATH"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return f"wrote {path}"

    import boto3

    key = cfg["DATA_KEY"]
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/geo+json",
        CacheControl="public, max-age=300",
    )
    dist = cfg.get("CLOUDFRONT_DISTRIBUTION_ID")
    if dist:
        import time

        boto3.client("cloudfront").create_invalidation(
            DistributionId=dist,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": [f"/{key}"]},
                "CallerReference": f"retina-{int(time.time() * 1000)}",
            },
        )
    return f"wrote s3://{bucket}/{key}"


def publish() -> str:
    """Invoke the publish Lambda if configured, otherwise publish inline."""
    fn = settings.RETINA.get("PUBLISH_FUNCTION_NAME")
    if fn:
        import boto3

        boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event", Payload=b"{}")
        return f"invoked {fn}"
    return publish_inline()
