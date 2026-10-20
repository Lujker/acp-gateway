"""Bounded public GHCR manifest discovery shared by CLI, inventory and host worker.

Only the official repository is trusted. No request supplies a URL, image or tag.
"""

import hashlib
import json
import platform
import re
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "ghcr.io/lujker/acp-gateway"
AUTH_URL = "https://ghcr.io/token?service=ghcr.io&scope=repository:lujker/acp-gateway:pull"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_INDEX = "application/vnd.docker.distribution.manifest.list.v2+json"
MEDIA = (MANIFEST, DOCKER_MANIFEST, INDEX, DOCKER_INDEX)
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
STABLE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
LIMIT = 65536


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Registry redirects are not allowed")


def _get(url, headers):
    prefix = "https://ghcr.io/v2/lujker/acp-gateway/manifests/"
    if url != AUTH_URL and not (
        isinstance(url, str)
        and url.startswith(prefix)
        and len(url[len(prefix) :]) <= 71
        and (STABLE.fullmatch(url[len(prefix) :]) or DIGEST.fullmatch(url[len(prefix) :]))
    ):
        raise ValueError("Refusing non-official registry endpoint")
    request = Request(url, headers=headers)  # noqa: S310 — fixed GHCR HTTPS URLs only
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ValueError("Registry response exceeded its limit")
        return body, response.headers


def validate_image_manifest(value):
    config = value.get("config")
    layers = value.get("layers")
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("digest"), str)
        or not DIGEST.fullmatch(config["digest"])
        or type(config.get("size")) is not int
        or not 0 < config["size"] <= 1048576
        or config.get("urls")
    ):
        raise ValueError("Invalid image manifest config")
    if not isinstance(layers, list) or len(layers) > 128:
        raise ValueError("Invalid image manifest layers")
    total = 0
    for layer in layers:
        if (
            not isinstance(layer, dict)
            or layer.get("urls")
            or not isinstance(layer.get("digest"), str)
            or not DIGEST.fullmatch(layer["digest"])
            or type(layer.get("size")) is not int
            or not 0 < layer["size"] <= 536870912
        ):
            raise ValueError("Invalid image manifest layer")
        total += layer["size"]
    if total > 2147483648:
        raise ValueError("Image manifest size exceeds its limit")


def official_image(version):
    """Return a content-verified immutable published image, or None if unpublished."""
    if not isinstance(version, str) or len(version) > 64 or not STABLE.fullmatch(version):
        raise ValueError("Docker delivery requires an exact stable version")
    token_body, _ = _get(AUTH_URL, {})
    token_value = json.loads(token_body)
    if not isinstance(token_value, dict):
        raise ValueError("Invalid public registry token response")
    token = token_value.get("token")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16384
        or any(ord(c) < 32 for c in token)
    ):
        raise ValueError("Invalid public registry token")
    headers = {"Authorization": "Bearer " + token, "Accept": ", ".join(MEDIA)}
    try:
        body, response_headers = _get(
            "https://ghcr.io/v2/lujker/acp-gateway/manifests/" + version, headers
        )
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if response_headers.get("Docker-Content-Digest") != digest:
        raise ValueError("Registry manifest digest mismatch")
    value = json.loads(body)
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != 2
        or value.get("mediaType") not in MEDIA
    ):
        raise ValueError("Unsupported registry manifest")
    if value["mediaType"] in {INDEX, DOCKER_INDEX}:
        architecture = {
            "x86_64": "amd64",
            "AMD64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }.get(platform.machine())
        entries = value.get("manifests")
        if not isinstance(entries, list) or len(entries) > 128:
            raise ValueError("Invalid image index")
        if any(
            not isinstance(entry, dict) or not isinstance(entry.get("platform"), dict)
            for entry in entries
        ):
            raise ValueError("Invalid index platform")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("platform", {}).get("os") == "linux"
            and entry.get("platform", {}).get("architecture") == architecture
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Ambiguous image platform")
        descriptor = matches[0]
        digest = descriptor.get("digest")
        if (
            not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or descriptor.get("mediaType") not in {MANIFEST, DOCKER_MANIFEST}
            or type(descriptor.get("size")) is not int
            or not 0 < descriptor["size"] <= LIMIT
        ):
            raise ValueError("Invalid platform image descriptor")
        child_body, child_headers = _get(
            "https://ghcr.io/v2/lujker/acp-gateway/manifests/" + digest, headers
        )
        if (
            len(child_body) != descriptor["size"]
            or "sha256:" + hashlib.sha256(child_body).hexdigest() != digest
            or child_headers.get("Docker-Content-Digest") != digest
        ):
            raise ValueError("Platform manifest digest mismatch")
        child = json.loads(child_body)
        if (
            not isinstance(child, dict)
            or child.get("schemaVersion") != 2
            or child.get("mediaType") not in {MANIFEST, DOCKER_MANIFEST}
        ):
            raise ValueError("Unsupported platform manifest")
        validate_image_manifest(child)
    else:
        # Published legacy single-manifest releases are Linux x86_64 only.
        # Other architectures require an explicit matching index descriptor.
        if platform.machine() not in {"x86_64", "AMD64", "amd64"}:
            return None
        validate_image_manifest(value)
    return REPOSITORY + "@" + digest
