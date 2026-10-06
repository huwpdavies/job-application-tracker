"""Microsoft sign-in (public client, personal accounts). Read-only scopes; token cache is per machine."""
from __future__ import annotations

import os

import msal

from .config import AUTHORITY, SCOPES, Config


class AuthError(RuntimeError):
    pass


def _load(cfg: Config) -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    if not cfg.client_id:
        raise AuthError("AZURE_CLIENT_ID is not set in .env. Follow SETUP.md first.")
    cache = msal.SerializableTokenCache()
    if cfg.token_cache_path.exists():
        cache.deserialize(cfg.token_cache_path.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(cfg.client_id, authority=AUTHORITY, token_cache=cache)
    return app, cache


def _save(cfg: Config, cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        cfg.token_cache_path.write_text(cache.serialize(), encoding="utf-8")
        try:
            os.chmod(cfg.token_cache_path, 0o600)
        except OSError:
            pass


def get_token(cfg: Config, interactive: bool = True, device_code: bool = False) -> str:
    """Return an access token, using the cache silently when possible.

    "offline_access" (refresh tokens) is added by MSAL itself; passing it explicitly is an error.
    """
    app, cache = _load(cfg)
    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        if not interactive:
            raise AuthError("Not signed in. Run: python -m tracker login")
        if device_code:
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise AuthError(f"Could not start device flow: {flow.get('error_description', flow)}")
            print(flow["message"])
            result = app.acquire_token_by_device_flow(flow)
        else:
            # Opens the system browser; redirect is http://localhost:<random port>.
            result = app.acquire_token_interactive(SCOPES, prompt="select_account")
    if "access_token" not in result:
        raise AuthError(f"Sign-in failed: {result.get('error')}: {result.get('error_description')}")
    _save(cfg, cache)
    return result["access_token"]


def sign_out(cfg: Config) -> None:
    if cfg.token_cache_path.exists():
        cfg.token_cache_path.unlink()
