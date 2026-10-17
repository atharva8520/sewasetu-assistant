# -*- coding: utf-8 -*-
"""
Authentication and Token Verification for Sewa Setu MCP Resource Servers.
RFC 9728 Protected Resource Metadata, RS256 JWKS verification, session liveness
checking via internal portal API, and token audience isolation.
"""

import time
import json
import logging
from typing import Any, Tuple, Optional
from urllib.parse import urlparse

import jwt
from jwt.algorithms import RSAAlgorithm
import httpx
from starlette.types import ASGIApp, Scope, Receive, Send
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.datastructures import Headers
from starlette.authentication import AuthCredentials

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.middleware.auth_context import auth_context_var

logger = logging.getLogger("sewasetu.mcp_auth")


class PortalTokenVerifier:
    def __init__(
        self,
        portal_internal_url: str,
        public_base_url: str,
        role: str,
        agent_internal_key: str,
    ):
        self.portal_internal_url = portal_internal_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.role = role
        self.agent_internal_key = agent_internal_key
        self.canonical_aud = f"{self.public_base_url}/mcp/{self.role}"
        self.metadata_url = (
            f"{self.public_base_url}/.well-known/oauth-protected-resource/mcp/{self.role}"
        )

        # Caches
        self._jwks_cache: dict[str, Any] = {}
        # (sub, client_id, aud) -> (is_live: bool, expires_at: float)
        self._session_cache: dict[Tuple[str, str, str], Tuple[bool, float]] = {}
        # jti -> (is_live: bool, expires_at: float)
        self._program_cache: dict[str, Tuple[bool, float]] = {}

    async def _fetch_jwks(self) -> None:
        """Fetch portal public keys from JWKS endpoint over internal network."""
        url = f"{self.portal_internal_url}/.well-known/jwks.json"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    keys = data.get("keys", [])
                    for k in keys:
                        kid = k.get("kid")
                        if kid:
                            pub_key = RSAAlgorithm.from_jwk(json.dumps(k))
                            self._jwks_cache[kid] = pub_key
        except Exception as e:
            logger.warning("Failed to fetch JWKS from %s: %s", url, type(e).__name__)

    async def _is_session_live(self, sub: str, client_id: str, aud: str) -> bool:
        """Query internal /session-live endpoint with <= 5-second TTL cache."""
        now = time.time()
        cache_key = (sub, client_id, aud)
        cached = self._session_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

        url = f"{self.portal_internal_url}/internal/agent/session-live"
        headers = {"X-Internal-Key": self.agent_internal_key}
        params = {"sub": sub, "client_id": client_id, "aud": aud}
        is_live = False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url, headers=headers, params=params)
                if r.status_code == 200:
                    data = r.json()
                    is_live = bool(data.get("live", False))
        except Exception as e:
            logger.warning("Session-live check failed: %s", type(e).__name__)
            is_live = False

        self._session_cache[cache_key] = (is_live, now + 5.0)
        return is_live

    async def _is_program_token_live(self, jti: str) -> bool:
        """Query internal /program-token-live for unattended officer tokens."""
        now = time.time()
        cached = self._program_cache.get(jti)
        if cached and cached[1] > now:
            return cached[0]

        url = f"{self.portal_internal_url}/internal/agent/program-token-live"
        headers = {"X-Internal-Key": self.agent_internal_key}
        params = {"jti": jti}
        is_live = False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url, headers=headers, params=params)
                if r.status_code == 200:
                    data = r.json()
                    is_live = bool(data.get("live", False))
        except Exception as e:
            logger.warning("Program token check failed: %s", type(e).__name__)
            is_live = False

        self._program_cache[jti] = (is_live, now + 5.0)
        return is_live

    async def verify(self, token: str) -> Tuple[Optional[AccessToken], str]:
        """
        Verify an incoming bearer token.
        Never logs token values.
        Returns:
            (access_token: AccessToken | None, error_code_or_reason: str)
        """
        if not token:
            return None, "missing_token"

        try:
            unverified_header = jwt.get_unverified_header(token)
        except Exception:
            return None, "malformed_token"

        if unverified_header.get("alg") != "RS256":
            return None, "unsupported_algorithm"

        kid = unverified_header.get("kid")
        if not kid:
            return None, "missing_kid"

        # Check key cache, refetch once if unknown kid
        if kid not in self._jwks_cache:
            await self._fetch_jwks()

        if kid not in self._jwks_cache:
            return None, "unknown_key_id"

        pub_key = self._jwks_cache[kid]

        # Decode JWT signature and expiration
        try:
            claims = jwt.decode(
                token,
                pub_key,
                algorithms=["RS256"],
                options={"verify_aud": False, "verify_iss": False, "verify_exp": True},
            )
        except jwt.ExpiredSignatureError:
            return None, "token_expired"
        except jwt.PyJWTError:
            return None, "invalid_signature"

        # Verify iss == PUBLIC_BASE_URL (exact match)
        if claims.get("iss") != self.public_base_url:
            return None, "issuer_mismatch"

        # Verify aud == THIS server's canonical URL (exact string match, string or list)
        token_aud = claims.get("aud")
        if isinstance(token_aud, str):
            if token_aud != self.canonical_aud:
                return None, "audience_mismatch"
        elif isinstance(token_aud, list):
            if self.canonical_aud not in token_aud:
                return None, "audience_mismatch"
        else:
            return None, "missing_audience"

        # Verify scope and role equal this server's role
        if claims.get("role") != self.role:
            return None, "role_mismatch"

        scope_raw = claims.get("scope", "")
        if isinstance(scope_raw, str):
            scopes = scope_raw.split()
        elif isinstance(scope_raw, list):
            scopes = scope_raw
        else:
            scopes = []

        if self.role not in scopes:
            return None, "insufficient_scope"

        kind = claims.get("kind", "access")
        # Citizen server rejects kind == 'program'
        if self.role == "citizen" and kind == "program":
            return None, "program_tokens_not_allowed"

        # Officer server accepts kind == 'program' only if the portal says it is not revoked
        if kind == "program":
            jti = claims.get("jti")
            if not jti or not await self._is_program_token_live(jti):
                return None, "program_token_revoked"
        else:
            # SESSION LIVENESS: check GET /internal/agent/session-live
            sub = claims.get("sub", "")
            client_id = claims.get("client_id", "")
            if not await self._is_session_live(sub, client_id, self.canonical_aud):
                return None, "session_ended"

        access_token = AccessToken(
            token=token,
            client_id=claims.get("client_id", ""),
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=self.canonical_aud,
            subject=claims.get("sub"),
            claims=claims,
        )
        return access_token, "ok"


class OAuthResourceServerMiddleware:
    """
    Starlette middleware enforcing RFC 9728 and OAuth 2.1 resource server security.
    """

    def __init__(self, app: ASGIApp, verifier: PortalTokenVerifier, mcp_path: str):
        self.app = app
        self.verifier = verifier
        self.mcp_path = mcp_path.rstrip("/")
        self.metadata_url = verifier.metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "").rstrip("/")

        # Allow healthz and protected resource metadata without auth
        if path == "/healthz" or path.startswith("/.well-known/oauth-protected-resource"):
            await self.app(scope, receive, send)
            return

        # Enforce auth on MCP routes
        if path == self.mcp_path or path.startswith(f"{self.mcp_path}/"):
            headers = Headers(scope=scope)
            auth_header = headers.get("authorization", "").strip()

            if not auth_header or not auth_header.lower().startswith("bearer "):
                # Missing token -> 401 with WWW-Authenticate header
                www_auth = f'Bearer resource_metadata="{self.metadata_url}"'
                resp = JSONResponse(
                    {
                        "error": "invalid_token",
                        "error_description": "Authentication required",
                        "message": "Authentication required",
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": www_auth},
                )
                await resp(scope, receive, send)
                return

            token = auth_header[7:].strip()
            access_token, reason = await self.verifier.verify(token)

            if not access_token:
                if reason == "session_ended":
                    desc = "sign-in ended, please sign in again"
                elif reason == "token_expired":
                    desc = "token has expired, please sign in again"
                elif reason == "audience_mismatch":
                    desc = "token not issued for this resource server"
                elif reason == "role_mismatch":
                    desc = "token does not have required role"
                else:
                    desc = f"invalid token ({reason})"

                www_auth = (
                    f'Bearer error="invalid_token", error_description="{desc}", '
                    f'resource_metadata="{self.metadata_url}"'
                )
                resp = JSONResponse(
                    {
                        "error": "invalid_token",
                        "error_description": desc,
                        "message": desc,
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": www_auth},
                )
                await resp(scope, receive, send)
                return

            # Successfully authenticated
            auth_user = AuthenticatedUser(access_token)
            scope["user"] = auth_user
            scope["auth"] = AuthCredentials(access_token.scopes)
            token_ctx = auth_context_var.set(auth_user)
            try:
                await self.app(scope, receive, send)
            finally:
                auth_context_var.reset(token_ctx)
            return

        # Fallback to next app
        await self.app(scope, receive, send)


def create_protected_resource_metadata_endpoint(
    public_base_url: str, role: str
):
    """
    Serve RFC 9728 Protected Resource Metadata at:
    /.well-known/oauth-protected-resource/mcp/<role>
    """
    canonical_url = f"{public_base_url.rstrip('/')}/mcp/{role}"
    metadata = {
        "resource": canonical_url,
        "authorization_servers": [public_base_url.rstrip("/")],
        "scopes_supported": [role],
        "bearer_methods_supported": ["header"],
    }

    async def metadata_handler(request):
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
        }
        if request.method == "OPTIONS":
            return Response("", status_code=204, headers=headers)
        return JSONResponse(metadata, headers=headers)

    return metadata_handler


def create_mcp_auth_app(
    server: MCPServer,
    role: str,
    portal_internal_url: str,
    public_base_url: str,
    agent_internal_key: str,
) -> ASGIApp:
    """
    Assemble the FastMCP Streamable HTTP app with RFC 9728 metadata,
    token verification, and session liveness middleware.
    """
    mcp_path = f"/mcp/{role}"
    verifier = PortalTokenVerifier(
        portal_internal_url=portal_internal_url,
        public_base_url=public_base_url,
        role=role,
        agent_internal_key=agent_internal_key,
    )

    # Health check route
    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request):
        return JSONResponse({"status": "ok", "role": role})

    # RFC 9728 metadata route
    meta_path = f"/.well-known/oauth-protected-resource/mcp/{role}"
    meta_handler = create_protected_resource_metadata_endpoint(public_base_url, role)

    @server.custom_route(meta_path, methods=["GET", "OPTIONS"])
    async def rfc9728_route(request):
        return await meta_handler(request)

    # Disable DNS rebinding rejection for reverse proxy compatibility
    transport_sec = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    )

    base_app = server.streamable_http_app(
        streamable_http_path=mcp_path,
        stateless_http=True,
        json_response=True,
        transport_security=transport_sec,
    )

    # Wrap with OAuthResourceServerMiddleware
    app = OAuthResourceServerMiddleware(
        app=base_app,
        verifier=verifier,
        mcp_path=mcp_path,
    )

    return app
