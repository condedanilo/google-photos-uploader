"""Thin httpx wrapper for the two Google Photos API calls.

Two operations:
  1. upload_bytes()   — POST raw bytes to get an upload token
  2. batch_create()   — POST tokens to register items in the library

Concurrency note:
  upload_bytes() is called from multiple worker threads.
  batch_create() is called from the main thread only (API serialization requirement).
  Both are synchronous; concurrency is managed by the orchestrator via ThreadPoolExecutor.

Retry logic is NOT implemented here — it lives in uploader/retry.py and is
applied by the caller (worker.py, orchestrator.py).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

import httpx  # type: ignore

from uploader.errors import (
    ApiError,
    NetworkError,
    QuotaExhaustedError,
    RateLimitError,
    ServerError,
)
from uploader.models import BatchCreateResult, UploadToken


_UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
_BATCH_CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
_ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"

# MIME type fallback for unknown extensions
_DEFAULT_MIME = "application/octet-stream"


class GooglePhotosClient:
    """Synchronous client for the Google Photos Library API."""

    def __init__(self, credentials, *, timeout: float = 120.0):
        """
        Args:
            credentials: google.oauth2.credentials.Credentials instance.
            timeout: HTTP timeout in seconds for individual requests.
        """
        self._creds = credentials
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upload_bytes(self, file_path: Path) -> str:
        """Upload raw file bytes and return the upload token.

        Args:
            file_path: Path to the file to upload (possibly a compressed temp file).

        Returns:
            Upload token string (used in batch_create).

        Raises:
            RateLimitError, QuotaExhaustedError, ServerError, NetworkError, ApiError.
        """
        mime_type = _get_mime_type(file_path)
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-type": "application/octet-stream",
            "X-Goog-Upload-Content-Type": mime_type,
            "X-Goog-Upload-Protocol": "raw",
        }

        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except OSError as e:
            from uploader.errors import FileReadError
            raise FileReadError(f"Cannot read {file_path}: {e}") from e

        try:
            response = httpx.post(
                _UPLOAD_URL,
                headers=headers,
                content=data,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as e:
            raise NetworkError(f"Upload timed out: {e}") from e
        except httpx.NetworkError as e:
            raise NetworkError(f"Network error during upload: {e}") from e

        _raise_for_status(response)
        return response.text.strip()

    def get_or_create_album(self, title: str) -> str:
        """Create a new Google Photos album and return its ID.

        Args:
            title: Display name for the album.

        Returns:
            The album ID string (used as ``albumId`` in batchCreate).

        Raises:
            RateLimitError, QuotaExhaustedError, ServerError, ApiError.
        """
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                _ALBUMS_URL,
                headers=headers,
                json={"album": {"title": title}},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as e:
            raise NetworkError(f"Album creation timed out: {e}") from e
        except httpx.NetworkError as e:
            raise NetworkError(f"Network error during album creation: {e}") from e

        _raise_for_status(response)
        return response.json()["id"]

    def batch_create(self, tokens: list[UploadToken], *, album_id: Optional[str] = None) -> list[BatchCreateResult]:
        """Register uploaded files in Google Photos via mediaItems:batchCreate.

        Args:
            tokens: List of UploadToken objects (max 50 per call, API limit).
            album_id: Optional album ID. When set, all items in this batch are
                added to that album. The album must have been created by this app.

        Returns:
            List of BatchCreateResult, one per input token, in the same order.

        Raises:
            RateLimitError, QuotaExhaustedError, ServerError, ApiError.
        """
        if not tokens:
            return []

        items = [
            {
                "description": "",
                "simpleMediaItem": {
                    "fileName": Path(t.original_path).name,
                    "uploadToken": t.token,
                },
            }
            for t in tokens
        ]

        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }

        body: dict = {"newMediaItems": items}
        if album_id:
            body["albumId"] = album_id

        try:
            response = httpx.post(
                _BATCH_CREATE_URL,
                headers=headers,
                json=body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as e:
            raise NetworkError(f"batchCreate timed out: {e}") from e
        except httpx.NetworkError as e:
            raise NetworkError(f"Network error during batchCreate: {e}") from e

        _raise_for_status(response)

        data = response.json()
        results: list[BatchCreateResult] = []
        api_results = data.get("newMediaItemResults", [])

        for i, item_result in enumerate(api_results):
            file_id = tokens[i].file_id if i < len(tokens) else -1
            status = item_result.get("status", {})
            code = status.get("code", 0)  # gRPC status code: 0 = OK
            if code == 0:
                media_item = item_result.get("mediaItem", {})
                results.append(BatchCreateResult(
                    file_id=file_id,
                    success=True,
                    google_media_id=media_item.get("id"),
                ))
            else:
                results.append(BatchCreateResult(
                    file_id=file_id,
                    success=False,
                    error_msg=status.get("message", f"gRPC error code {code}"),
                ))

        # If API returned fewer results than tokens (shouldn't happen), fill with errors
        for i in range(len(api_results), len(tokens)):
            results.append(BatchCreateResult(
                file_id=tokens[i].file_id,
                success=False,
                error_msg="Missing result from batchCreate response",
            ))

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        from google.auth.transport.requests import Request  # type: ignore
        from google.auth.exceptions import RefreshError  # type: ignore
        from uploader.errors import TokenRefreshError

        if self._creds.expired and self._creds.refresh_token:
            try:
                self._creds.refresh(Request())
            except RefreshError as e:
                raise TokenRefreshError(str(e)) from e

        return self._creds.token


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------

def _raise_for_status(response: httpx.Response) -> None:
    """Map HTTP error responses to domain exceptions."""
    if response.is_success:
        return

    status = response.status_code

    # Try to parse structured error body
    reason: Optional[str] = None
    try:
        body = response.json()
        error = body.get("error", {})
        reason = error.get("status") or error.get("message")
        # Check for quota errors in errors array
        for err_detail in error.get("errors", []):
            if err_detail.get("reason") in ("dailyLimitExceeded", "quotaExceeded",
                                             "userRateLimitExceeded"):
                if err_detail.get("reason") == "dailyLimitExceeded":
                    raise QuotaExhaustedError()
    except (ValueError, KeyError):
        pass

    if status == 429:
        retry_after: Optional[float] = None
        ra_header = response.headers.get("Retry-After")
        if ra_header:
            try:
                retry_after = float(ra_header)
            except ValueError:
                pass
        raise RateLimitError(retry_after=retry_after)

    if 500 <= status < 600:
        raise ServerError(status, f"Server error: {response.text[:200]}")

    raise ApiError(status, f"API error {status}: {response.text[:200]}", reason=reason)


def _get_mime_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or _DEFAULT_MIME
