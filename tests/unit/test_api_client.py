"""Unit tests for uploader/api_client.py using respx to mock httpx calls."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from uploader.api_client import GooglePhotosClient, _ALBUMS_URL, _BATCH_CREATE_URL, _UPLOAD_URL
from uploader.errors import (
    ApiError,
    NetworkError,
    QuotaExhaustedError,
    RateLimitError,
    ServerError,
)
from uploader.models import UploadToken


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_creds():
    creds = MagicMock()
    creds.token = "fake-access-token"
    creds.expired = False
    creds.refresh_token = "fake-refresh-token"
    return creds


@pytest.fixture
def client(fake_creds) -> GooglePhotosClient:
    return GooglePhotosClient(fake_creds, timeout=5.0)


@pytest.fixture
def small_file(tmp_path: Path) -> Path:
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"fake jpeg bytes")
    return p


# ---------------------------------------------------------------------------
# upload_bytes
# ---------------------------------------------------------------------------

class TestUploadBytes:
    @respx.mock
    def test_returns_token_on_success(self, client, small_file):
        respx.post(_UPLOAD_URL).mock(return_value=httpx.Response(200, text="upload-token-xyz"))
        token = client.upload_bytes(small_file)
        assert token == "upload-token-xyz"

    @respx.mock
    def test_raises_rate_limit_on_429(self, client, small_file):
        respx.post(_UPLOAD_URL).mock(return_value=httpx.Response(429))
        with pytest.raises(RateLimitError):
            client.upload_bytes(small_file)

    @respx.mock
    def test_retry_after_header_parsed(self, client, small_file):
        respx.post(_UPLOAD_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        )
        with pytest.raises(RateLimitError) as exc_info:
            client.upload_bytes(small_file)
        assert exc_info.value.retry_after == 30.0

    @respx.mock
    def test_raises_server_error_on_5xx(self, client, small_file):
        respx.post(_UPLOAD_URL).mock(return_value=httpx.Response(503, text="Service unavailable"))
        with pytest.raises(ServerError):
            client.upload_bytes(small_file)

    @respx.mock
    def test_raises_api_error_on_4xx(self, client, small_file):
        respx.post(_UPLOAD_URL).mock(return_value=httpx.Response(400, text="Bad request"))
        with pytest.raises(ApiError):
            client.upload_bytes(small_file)

    @respx.mock
    def test_network_error_on_connection_failure(self, client, small_file):
        respx.post(_UPLOAD_URL).mock(side_effect=httpx.NetworkError("connection refused"))
        with pytest.raises(NetworkError):
            client.upload_bytes(small_file)


# ---------------------------------------------------------------------------
# batch_create
# ---------------------------------------------------------------------------

def _batch_success_response(file_ids: list[int]) -> dict:
    return {
        "newMediaItemResults": [
            {
                "uploadToken": f"tok-{fid}",
                "status": {"code": 0, "message": "OK"},
                "mediaItem": {"id": f"media-{fid}", "filename": f"img-{fid}.jpg"},
            }
            for fid in file_ids
        ]
    }


class TestBatchCreate:
    @respx.mock
    def test_returns_success_results(self, client):
        tokens = [UploadToken(file_id=1, token="tok-1", original_path="/a.jpg"),
                  UploadToken(file_id=2, token="tok-2", original_path="/b.jpg")]
        respx.post(_BATCH_CREATE_URL).mock(
            return_value=httpx.Response(200, json=_batch_success_response([1, 2]))
        )
        results = client.batch_create(tokens)
        assert len(results) == 2
        assert all(r.success for r in results)
        assert results[0].google_media_id == "media-1"
        assert results[1].google_media_id == "media-2"

    @respx.mock
    def test_empty_tokens_returns_empty(self, client):
        results = client.batch_create([])
        assert results == []

    @respx.mock
    def test_per_item_error_marked_as_failure(self, client):
        tokens = [UploadToken(file_id=1, token="tok-1", original_path="/a.jpg")]
        respx.post(_BATCH_CREATE_URL).mock(
            return_value=httpx.Response(200, json={
                "newMediaItemResults": [{
                    "status": {"code": 3, "message": "invalid argument"},
                }]
            })
        )
        results = client.batch_create(tokens)
        assert len(results) == 1
        assert not results[0].success
        assert "invalid argument" in results[0].error_msg

    @respx.mock
    def test_raises_quota_exhausted(self, client):
        tokens = [UploadToken(file_id=1, token="tok-1", original_path="/a.jpg")]
        body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                          "errors": [{"reason": "dailyLimitExceeded"}]}}
        respx.post(_BATCH_CREATE_URL).mock(
            return_value=httpx.Response(429, json=body)
        )
        with pytest.raises(QuotaExhaustedError):
            client.batch_create(tokens)

    @respx.mock
    def test_raises_rate_limit_on_429(self, client):
        tokens = [UploadToken(file_id=1, token="tok-1", original_path="/a.jpg")]
        respx.post(_BATCH_CREATE_URL).mock(return_value=httpx.Response(429))
        with pytest.raises(RateLimitError):
            client.batch_create(tokens)

    @respx.mock
    def test_album_id_included_in_request_body(self, client):
        tokens = [UploadToken(file_id=1, token="tok-1", original_path="/a.jpg")]
        route = respx.post(_BATCH_CREATE_URL).mock(
            return_value=httpx.Response(200, json=_batch_success_response([1]))
        )
        client.batch_create(tokens, album_id="album-abc-123")
        sent_body = route.calls[0].request.content
        import json
        parsed = json.loads(sent_body)
        assert parsed["albumId"] == "album-abc-123"

    @respx.mock
    def test_no_album_id_omits_field(self, client):
        tokens = [UploadToken(file_id=1, token="tok-1", original_path="/a.jpg")]
        route = respx.post(_BATCH_CREATE_URL).mock(
            return_value=httpx.Response(200, json=_batch_success_response([1]))
        )
        client.batch_create(tokens)
        import json
        parsed = json.loads(route.calls[0].request.content)
        assert "albumId" not in parsed


# ---------------------------------------------------------------------------
# get_or_create_album
# ---------------------------------------------------------------------------

class TestGetOrCreateAlbum:
    @respx.mock
    def test_returns_album_id_on_success(self, client):
        respx.post(_ALBUMS_URL).mock(
            return_value=httpx.Response(200, json={"id": "album-xyz", "title": "My Upload"})
        )
        album_id = client.get_or_create_album("My Upload")
        assert album_id == "album-xyz"

    @respx.mock
    def test_raises_server_error_on_5xx(self, client):
        respx.post(_ALBUMS_URL).mock(return_value=httpx.Response(500, text="error"))
        with pytest.raises(ServerError):
            client.get_or_create_album("My Upload")

    @respx.mock
    def test_raises_network_error_on_timeout(self, client):
        respx.post(_ALBUMS_URL).mock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(NetworkError):
            client.get_or_create_album("My Upload")
