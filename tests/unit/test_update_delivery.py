"""Official registry lookup is bounded, read-only, and pinned before pull."""

import hashlib
import json

import pytest


def manifest():
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "digest": "sha256:" + "a" * 64,
                "size": 100,
                "mediaType": "application/vnd.oci.image.config.v1+json",
            },
            "layers": [],
        }
    ).encode()


def test_official_version_resolves_only_verified_content_digest(monkeypatch):
    from acp_gateway import update_delivery as delivery

    body = manifest()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    calls = []

    def get(url, headers):
        calls.append((url, headers))
        if "/token?" in url:
            return json.dumps({"token": "public-token"}).encode(), {}
        return body, {"Docker-Content-Digest": digest}

    monkeypatch.setattr(delivery, "_get", get)
    assert delivery.official_image("0.3.0") == "ghcr.io/lujker/acp-gateway@" + digest
    assert calls[1][0] == "https://ghcr.io/v2/lujker/acp-gateway/manifests/0.3.0"
    assert calls[1][1]["Authorization"] == "Bearer public-token"


@pytest.mark.parametrize(
    "platform",
    [{"os": "linux", "architecture": "arm64"}, {"os": "windows", "architecture": "amd64"}],
)
def test_unpublished_host_platform_is_not_offered(monkeypatch, platform):
    from acp_gateway import update_delivery as delivery

    body = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": delivery.INDEX,
            "manifests": [
                {
                    "mediaType": delivery.MANIFEST,
                    "digest": "sha256:" + "b" * 64,
                    "size": 300,
                    "platform": platform,
                }
            ],
        }
    ).encode()
    monkeypatch.setattr(delivery.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        delivery,
        "_get",
        lambda url, headers: (
            (b'{"token":"public"}', {})
            if "/token?" in url
            else (body, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(body).hexdigest()})
        ),
    )
    assert delivery.official_image("0.3.0") is None


def test_index_child_is_fetched_and_content_verified_before_offering(monkeypatch):
    from acp_gateway import update_delivery as delivery

    child = manifest()
    child_digest = "sha256:" + hashlib.sha256(child).hexdigest()
    body = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": delivery.INDEX,
            "manifests": [
                {
                    "mediaType": delivery.MANIFEST,
                    "digest": child_digest,
                    "size": len(child),
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ],
        }
    ).encode()
    calls = []

    def get(url, headers):
        calls.append(url)
        if "/token?" in url:
            return b'{"token":"public"}', {}
        data = child if url.endswith(child_digest) else body
        return data, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(data).hexdigest()}

    monkeypatch.setattr(delivery, "_get", get)
    monkeypatch.setattr(delivery.platform, "machine", lambda: "x86_64")
    assert delivery.official_image("0.3.0") == delivery.REPOSITORY + "@" + child_digest
    assert calls[-1].endswith("/manifests/" + child_digest)


@pytest.mark.parametrize("kind", ["foreign-layer", "missing-config", "too-many-layers"])
def test_malformed_or_external_layer_manifests_are_not_offered(monkeypatch, kind):
    from acp_gateway import update_delivery as delivery

    value = json.loads(manifest())
    if kind == "foreign-layer":
        value["layers"] = [
            {"digest": "sha256:" + "a" * 64, "urls": ["https://untrusted.invalid/blob"]}
        ]
    elif kind == "missing-config":
        value.pop("config")
    else:
        value["layers"] = [{}] * 129
    body = json.dumps(value).encode()
    monkeypatch.setattr(
        delivery,
        "_get",
        lambda url, headers: (
            (b'{"token":"public"}', {})
            if "/token?" in url
            else (body, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(body).hexdigest()})
        ),
    )
    with pytest.raises(ValueError, match="manifest"):
        delivery.official_image("0.3.0")


@pytest.mark.parametrize("body", [b"[]", b"null", b'"not-object"'])
def test_invalid_token_response_is_a_controlled_failure(monkeypatch, body):
    from acp_gateway import update_delivery as delivery

    monkeypatch.setattr(delivery, "_get", lambda *args: (body, {}))
    with pytest.raises(ValueError, match="token"):
        delivery.official_image("0.3.0")


def test_legacy_single_platform_image_is_not_offered_to_arm_hosts(monkeypatch):
    from acp_gateway import update_delivery as delivery

    body = manifest()
    monkeypatch.setattr(delivery.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        delivery,
        "_get",
        lambda url, headers: (
            (b'{"token":"public"}', {})
            if "/token?" in url
            else (body, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(body).hexdigest()})
        ),
    )
    assert delivery.official_image("0.3.0") is None


@pytest.mark.parametrize(
    "url",
    [
        "file:///private",
        "https://untrusted.invalid/manifest",
        "https://ghcr.io/evil",
        "https://ghcr.io/v2/lujker/acp-gateway/manifests/../evil",
    ],
)
def test_transport_refuses_any_non_official_endpoint_before_io(monkeypatch, url):
    from acp_gateway import update_delivery as delivery

    called = []
    monkeypatch.setattr(delivery, "build_opener", lambda *args: called.append(True))
    with pytest.raises(ValueError, match="endpoint"):
        delivery._get(url, {})
    assert called == []


@pytest.mark.parametrize("platform_value", [None, [], "linux/amd64"])
def test_malformed_index_platform_is_a_controlled_failure(monkeypatch, platform_value):
    from acp_gateway import update_delivery as delivery

    body = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": delivery.INDEX,
            "manifests": [{"platform": platform_value}],
        }
    ).encode()
    monkeypatch.setattr(
        delivery,
        "_get",
        lambda url, headers: (
            (b'{"token":"public"}', {})
            if "/token?" in url
            else (body, {"Docker-Content-Digest": "sha256:" + hashlib.sha256(body).hexdigest()})
        ),
    )
    with pytest.raises(ValueError, match="platform"):
        delivery.official_image("0.3.0")
