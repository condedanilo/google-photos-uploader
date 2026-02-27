"""OAuth 2.0 authentication for Google Photos API.

Flow:
  1. Load existing token from token_path (JSON)
  2. If expired, refresh using the stored refresh_token
  3. If no token or refresh fails, run the InstalledAppFlow (opens browser)
  4. Persist updated token back to token_path

The token file is written with 0o600 permissions on POSIX systems to prevent
other users on the same machine from reading the credentials.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional

from uploader.errors import AuthError, TokenRefreshError

# Scopes — minimum required for uploading only (write-only, cannot read/delete)
SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly"]


def get_credentials(
    credentials_path: Path,
    token_path: Path,
    *,
    interactive: bool = True,
):
    """Return valid Google credentials, refreshing or re-authorizing as needed.

    Args:
        credentials_path: Path to the OAuth client_secret.json from Google Cloud.
        token_path: Path where the access token is persisted.
        interactive: If False, raises AuthError when no valid token exists
            (useful in non-interactive environments).

    Returns:
        google.oauth2.credentials.Credentials

    Raises:
        AuthError: if credentials_path doesn't exist or auth fails.
        TokenRefreshError: if token refresh fails and interactive=False.
    """
    from google.auth.exceptions import RefreshError  # type: ignore
    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore

    if not credentials_path.exists():
        raise AuthError(
            f"Credentials file not found: {credentials_path}\n"
            "Download client_secret.json from Google Cloud Console and place it there.\n"
            "See README.md for setup instructions."
        )

    creds: Optional[Credentials] = None

    # Step 1: Load existing token
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, KeyError, json.JSONDecodeError):
            # Corrupted token file — proceed to re-auth
            creds = None

    # Step 2: Refresh if expired
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, token_path)
            return creds
        except RefreshError as e:
            if not interactive:
                raise TokenRefreshError(
                    f"Token refresh failed: {e}. Run: gphotos auth login"
                ) from e
            # Fall through to re-auth
            creds = None

    if creds and creds.valid:
        return creds

    # Step 3: Interactive OAuth flow
    if not interactive:
        raise TokenRefreshError(
            "No valid token found and running in non-interactive mode. "
            "Run: gphotos auth login"
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            open_browser=True,
            success_message=(
                "Authentication successful! You can close this window and return to the terminal."
            ),
        )
    except Exception as e:
        raise AuthError(f"OAuth flow failed: {e}") from e

    _save_token(creds, token_path)
    return creds


def token_info(token_path: Path) -> dict:
    """Return a summary of the stored token for display purposes.

    Returns a dict with keys: valid (bool), expired (bool), expiry (str|None).
    """
    from google.oauth2.credentials import Credentials  # type: ignore

    if not token_path.exists():
        return {"valid": False, "expired": None, "expiry": None}

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        return {
            "valid": creds.valid,
            "expired": creds.expired,
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
        }
    except Exception:
        return {"valid": False, "expired": None, "expiry": None}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _save_token(creds, token_path: Path) -> None:
    """Persist credentials to token_path with restrictive permissions."""
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    # Restrict to owner-only on POSIX (macOS, Linux)
    if os.name == "posix":
        token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
