#!/usr/bin/env python3
"""Provision the mcsd bucket's AbortIncompleteMultipartUpload lifecycle rule.

Run as a one-shot compose service (mirrors ``migrate``) against the in-compose
SeaweedFS S3 gateway, using the same ``MCD_API_STORAGE__OBJECT__*`` env as the
api service. It is the storage-layer backstop the object sweep's own comments
recommend (``object_store.py`` ``MultipartUploadsUnsupportedError`` /
``_sweep_multipart``): a hard crash after ``CreateMultipartUpload`` but before
the first part leaves an entry the app sweep cannot age-gate, so a bucket
lifecycle rule reclaims it instead (docs/dev/DEPLOYMENT.md Section 5).

The rule's window (``DAYS_AFTER_INITIATION``) sits safely above the app sweep's
1h/24h age thresholds so it never races the app-level abort.

Sync boto3 (shipped in the ``mcsd-api:dev`` image via ``aioboto3``), no new
dependency. The script:

1. creates the bucket if absent,
2. puts the AbortIncompleteMultipartUpload lifecycle rule,
3. **self-verifies** by reading it back and exits non-zero with a clear message
   if the rule did not round-trip — this is the SeaweedFS-support gate, so the
   service fails loudly rather than silently no-op'ing (issue #2260).

Self-verify proves the config was *accepted and persisted*, not that an abort
actually executed (``DaysAfterInitiation`` is integer-days, so real execution is
not observable in CI); acceptance is the best that is verifiable here.
"""

from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError

# Endpoint defaults to the in-compose S3 gateway (hardcoded elsewhere in
# compose); the compose service passes it explicitly via the api-mirrored env.
ENDPOINT = os.environ.get("MCD_API_STORAGE__OBJECT__ENDPOINT", "http://seaweedfs:8333")
BUCKET = os.environ.get("MCD_API_STORAGE__OBJECT__BUCKET", "mcsd")

# Integer days (S3 lifecycle granularity). 2 is safely above the app sweep's
# 1h abort / 24h thresholds so the rule never fights the app-level abort.
DAYS_AFTER_INITIATION = 2
RULE_ID = "abort-incomplete-multipart-uploads"


def _fail(message: str) -> None:
    print(f"FATAL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    access_key = os.environ.get("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "")
    secret_key = os.environ.get("MCD_API_STORAGE__OBJECT__SECRET_KEY", "")
    if not access_key or not secret_key:
        _fail(
            "MCD_API_STORAGE__OBJECT__ACCESS_KEY/SECRET_KEY must be set to "
            "non-empty values (see .env.example)"
        )

    # Minimal client config, mirroring the api's aioboto3 session (keys +
    # endpoint); region is required by botocore's signer but unused by SeaweedFS.
    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )

    # 1. Create the bucket if absent (SeaweedFS otherwise only creates it on the
    # first write, so there would be nothing to attach the rule to).
    try:
        s3.head_bucket(Bucket=BUCKET)
        print(f"bucket {BUCKET!r} already exists")
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)
        print(f"created bucket {BUCKET!r}")

    # 2. Put the AbortIncompleteMultipartUpload lifecycle rule.
    s3.put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": RULE_ID,
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {
                        "DaysAfterInitiation": DAYS_AFTER_INITIATION
                    },
                }
            ]
        },
    )
    print(
        f"put lifecycle rule {RULE_ID!r} "
        f"(DaysAfterInitiation={DAYS_AFTER_INITIATION}) on {BUCKET!r}"
    )

    # 3. Self-verify: read it back and confirm the rule round-tripped. SeaweedFS
    # support for this action is the gate — fail loudly rather than no-op.
    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=BUCKET).get("Rules", [])
    except ClientError as exc:
        _fail(
            "lifecycle rule was not honored: GetBucketLifecycleConfiguration "
            f"failed after the put ({exc}). The object store does not support "
            "AbortIncompleteMultipartUpload; see docs/dev/DEPLOYMENT.md Section 5."
        )

    honored = any(
        rule.get("AbortIncompleteMultipartUpload", {}).get("DaysAfterInitiation")
        == DAYS_AFTER_INITIATION
        for rule in rules
    )
    if not honored:
        _fail(
            "lifecycle rule did not round-trip: the AbortIncompleteMultipartUpload "
            "rule is absent from GetBucketLifecycleConfiguration. The object store "
            "does not honor it; see docs/dev/DEPLOYMENT.md Section 5."
        )

    print("self-verify OK: AbortIncompleteMultipartUpload rule round-tripped")


if __name__ == "__main__":
    main()
