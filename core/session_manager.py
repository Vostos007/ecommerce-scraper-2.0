"""
Advanced session management system with encryption and persistence.
Handles cookie persistence, session data management, and security.
"""

import asyncio
import aiofiles
import json
import os
import hashlib
from typing import Dict, Optional, Any, List, Union
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class SessionData:
    """Session data structure with metadata."""

    domain: str
    cookies: Dict[str, str]
    headers: Dict[str, str]
    auth_tokens: Dict[str, str]
    user_agent: Optional[str] = None
    created_at: Optional[datetime] = None
    last_accessed: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    is_authenticated: bool = False
    session_id: Optional[str] = None
    csrf_token: Optional[str] = None
    custom_data: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.last_accessed is None:
            self.last_accessed = datetime.now()
        if self.custom_data is None:
            self.custom_data = {}

    def update_access_time(self):
        """Update last accessed timestamp."""
        self.last_accessed = datetime.now()

    def is_expired(self) -> bool:
        """Check if session has expired."""
        if self.expires_at is None:
            return False
        return datetime.now() > self.expires_at

    def is_valid(self) -> bool:
        """Check if session is valid (not expired and has basic data)."""
        return not self.is_expired() and bool(self.cookies or self.auth_tokens)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        # Convert datetime objects to ISO strings
        for field in ["created_at", "last_accessed", "expires_at"]:
            if data[field]:
                data[field] = data[field].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionData":
        """Create SessionData from dictionary."""
        # Remove internal fields that shouldn't be passed to constructor
        data_copy = data.copy()
        data_copy.pop("_domain", None)

        # Convert ISO strings back to datetime objects
        for field in ["created_at", "last_accessed", "expires_at"]:
            if data_copy.get(field):
                data_copy[field] = datetime.fromisoformat(data_copy[field])
        return cls(**data_copy)


class SessionManager:
    """Advanced session manager with encryption and persistence."""

    def __init__(self, config: Dict):
        self.config = config
        self.session_dir = Path(config.get("session_dir", "data/sessions"))
        self.session_ttl = config.get("session_ttl_seconds", 3600)  # 1 hour default
        self.encryption_enabled = config.get("encryption_enabled", True)
        self.auto_cleanup = config.get("auto_cleanup_expired", True)
        self.cleanup_interval = config.get(
            "cleanup_interval_seconds", 1800
        )  # 30 minutes
        self.max_sessions_per_domain = config.get("max_sessions_per_domain", 10)

        # Session validation config
        validation_config = config.get("session_validation", {})
        self.validate_on_load = validation_config.get("validate_on_load", True)
        self.refresh_threshold = validation_config.get("refresh_threshold_seconds", 300)
        self.auto_refresh = validation_config.get("auto_refresh", True)

        # In-memory session cache
        self.sessions_cache: Dict[str, SessionData] = {}
        self._cache_lock = asyncio.Lock()

        # Ensure session directory exists
        self.session_dir.mkdir(parents=True, exist_ok=True)

        # Set proper permissions for session directory (700 = owner only)
        os.chmod(self.session_dir, 0o700)

        # Encryption setup
        self.encryption_key = None
        if self.encryption_enabled:
            self._setup_encryption()

        logger.info(f"SessionManager initialized with directory: {self.session_dir}")

        # Store cleanup task reference for later start
        self._cleanup_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start background tasks if auto cleanup is enabled."""
        if self.auto_cleanup:
            try:
                # Only create task if we have a running event loop
                asyncio.get_running_loop()
                self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
                logger.info("SessionManager background cleanup started")
            except RuntimeError:
                logger.warning(
                    "No event loop running, SessionManager background cleanup will start when needed"
                )

    def _setup_encryption(self):
        """Setup encryption key for session data."""
        key_file = self.session_dir / ".session_key"

        if key_file.exists():
            # Load existing key
            with open(key_file, "rb") as f:
                self.encryption_key = f.read()
        else:
            # Generate new key
            password = os.environ.get(
                "SESSION_PASSWORD", "default-password-change-me"
            ).encode()
            salt = os.urandom(16)

            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=salt,
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(password))

            # Save key and salt
            with open(key_file, "wb") as f:
                f.write(key)

            # Set restrictive permissions on key file
            os.chmod(key_file, 0o600)

            self.encryption_key = key

        logger.info("Session encryption initialized")

    def _encrypt_data(self, data: str) -> str:
        """Encrypt session data."""
        if not self.encryption_enabled:
            return data

        fernet = Fernet(self.encryption_key)
        encrypted_data = fernet.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted_data).decode()

    def _decrypt_data(self, encrypted_data: str) -> str:
        """Decrypt session data."""
        if not self.encryption_enabled:
            return encrypted_data

        try:
            fernet = Fernet(self.encryption_key)
            decoded_data = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_data = fernet.decrypt(decoded_data)
            return decrypted_data.decode()
        except Exception as e:
            logger.error(f"Failed to decrypt session data: {e}")
            raise

    async def save_session(
        self, domain: str, session_data: Union[Dict[str, Any], "SessionData"]
    ) -> bool:
        """
        Save session data for a domain.

        Args:
            domain: Domain name
            session_data: Session data to save

        Returns:
            True if saved successfully
        """
        try:
            # Create or update SessionData object
            if isinstance(session_data, SessionData):
                session = session_data
            else:
                # Create SessionData from dictionary
                session = SessionData(
                    domain=domain,
                    cookies=session_data.get("cookies", {}),
                    headers=session_data.get("headers", {}),
                    auth_tokens=session_data.get("auth_tokens", {}),
                    user_agent=session_data.get("user_agent"),
                    is_authenticated=session_data.get("is_authenticated", False),
                    session_id=session_data.get("session_id"),
                    csrf_token=session_data.get("csrf_token"),
                    custom_data=session_data.get("custom_data", {}),
                )

            # Set expiration time only if not already set or if it's a dict
            if isinstance(session_data, dict) or session.expires_at is None:
                session.expires_at = datetime.now() + timedelta(
                    seconds=self.session_ttl
                )
            session.update_access_time()

            # Save to cache
            async with self._cache_lock:
                cache_key = self._get_session_key(domain)
                self.sessions_cache[cache_key] = session

            # Save to file
            await self._save_session_to_file(domain, session)

            logger.debug(f"Session saved for domain: {domain}")
            return True

        except Exception as e:
            logger.error(f"Failed to save session for {domain}: {e}")
            return False

    async def load_session(self, domain: str) -> Optional[SessionData]:
        """
        Load session data for a domain.

        Args:
            domain: Domain name

        Returns:
            SessionData object or None if not found/invalid
        """
        try:
            cache_key = self._get_session_key(domain)

            # Try cache first
            async with self._cache_lock:
                if cache_key in self.sessions_cache:
                    session = self.sessions_cache[cache_key]
                    if self.validate_on_load and not session.is_valid():
                        del self.sessions_cache[cache_key]
                        await self._delete_session_file(domain)
                        return None
                    session.update_access_time()
                    return session

            # Load from file
            session = await self._load_session_from_file(domain)
            if session:
                # Validate session
                if self.validate_on_load and not session.is_valid():
                    await self._delete_session_file(domain)
                    return None

                # Add to cache
                async with self._cache_lock:
                    self.sessions_cache[cache_key] = session

                session.update_access_time()
                logger.debug(f"Session loaded for domain: {domain}")
                return session

            return None

        except Exception as e:
            logger.error(f"Failed to load session for {domain}: {e}")
            return None

    async def update_session(
        self,
        domain: str,
        cookies: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        auth_tokens: Optional[Dict[str, str]] = None,
        custom_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Update existing session data.

        Args:
            domain: Domain name
            cookies: Cookies to update
            headers: Headers to update
            auth_tokens: Auth tokens to update
            custom_data: Custom data to update

        Returns:
            True if updated successfully
        """
        session = await self.load_session(domain)
        if not session:
            # Create new session if doesn't exist
            session_data = {
                "cookies": cookies or {},
                "headers": headers or {},
                "auth_tokens": auth_tokens or {},
                "custom_data": custom_data or {},
            }
            return await self.save_session(domain, session_data)

        # Update existing session
        if cookies:
            session.cookies.update(cookies)
        if headers:
            session.headers.update(headers)
        if auth_tokens:
            session.auth_tokens.update(auth_tokens)
        if custom_data and session.custom_data:
            session.custom_data.update(custom_data)

        # Update metadata
        session.update_access_time()
        session.expires_at = datetime.now() + timedelta(seconds=self.session_ttl)

        return await self.save_session(domain, session)

    async def clear_expired_sessions(self) -> int:
        """
        Clear all expired sessions.

        Returns:
            Number of sessions cleared
        """
        cleared_count = 0

        try:
            # Clear from cache
            async with self._cache_lock:
                expired_keys = []
                for key, session in self.sessions_cache.items():
                    if session.is_expired():
                        expired_keys.append(key)

                for key in expired_keys:
                    del self.sessions_cache[key]
                    cleared_count += 1

            # Clear from files
            if self.session_dir.exists():
                for session_file in self.session_dir.glob("*.json"):
                    try:
                        # Load session data directly from file to get domain
                        async with aiofiles.open(
                            session_file, "r", encoding="utf-8"
                        ) as f:
                            json_data = await f.read()

                        if self.encryption_enabled:
                            json_data = self._decrypt_data(json_data)

                        session_dict = json.loads(json_data)
                        domain = session_dict.get("_domain")

                        if domain:
                            session = SessionData.from_dict(session_dict)
                            if session and session.is_expired():
                                session_file.unlink()
                                cleared_count += 1
                    except Exception as e:
                        logger.warning(
                            f"Error checking session file {session_file}: {e}"
                        )

            if cleared_count > 0:
                logger.info(f"Cleared {cleared_count} expired sessions")

        except Exception as e:
            logger.error(f"Error clearing expired sessions: {e}")

        return cleared_count

    def is_session_valid(self, domain: str) -> bool:
        """
        Check if session is valid without loading it.

        Args:
            domain: Domain name

        Returns:
            True if session exists and is valid
        """
        cache_key = self._get_session_key(domain)
        if cache_key in self.sessions_cache:
            return self.sessions_cache[cache_key].is_valid()

        # Check file timestamp as quick validation
        session_file = self.session_dir / f"{self._sanitize_domain(domain)}.json"
        if session_file.exists():
            # Check file modification time
            file_age = datetime.now() - datetime.fromtimestamp(
                session_file.stat().st_mtime
            )
            return file_age.total_seconds() < self.session_ttl

        return False

    def get_session_cookies(self, domain: str) -> Dict[str, str]:
        """
        Get cookies for a domain synchronously from cache.

        Args:
            domain: Domain name

        Returns:
            Dictionary of cookies
        """
        cache_key = self._get_session_key(domain)
        if cache_key in self.sessions_cache:
            session = self.sessions_cache[cache_key]
            if session.is_valid():
                return session.cookies.copy()

        return {}

    def get_session_headers(self, domain: str) -> Dict[str, str]:
        """
        Get headers for a domain synchronously from cache.

        Args:
            domain: Domain name

        Returns:
            Dictionary of headers
        """
        cache_key = self._get_session_key(domain)
        if cache_key in self.sessions_cache:
            session = self.sessions_cache[cache_key]
            if session.is_valid():
                return session.headers.copy()

        return {}

    async def delete_session(self, domain: str) -> bool:
        """
        Delete session for a domain.

        Args:
            domain: Domain name

        Returns:
            True if deleted successfully
        """
        try:
            # Remove from cache
            cache_key = self._get_session_key(domain)
            async with self._cache_lock:
                if cache_key in self.sessions_cache:
                    del self.sessions_cache[cache_key]

            # Remove file
            await self._delete_session_file(domain)

            logger.debug(f"Session deleted for domain: {domain}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete session for {domain}: {e}")
            return False

    async def list_active_sessions(self) -> List[Dict[str, Any]]:
        """
        List all active (non-expired) sessions.

        Returns:
            List of session information
        """
        active_sessions = []

        # Get from cache
        async with self._cache_lock:
            for session in self.sessions_cache.values():
                if session.is_valid():
                    active_sessions.append(
                        {
                            "domain": session.domain,
                            "created_at": (
                                session.created_at.isoformat()
                                if session.created_at
                                else None
                            ),
                            "last_accessed": (
                                session.last_accessed.isoformat()
                                if session.last_accessed
                                else None
                            ),
                            "expires_at": (
                                session.expires_at.isoformat()
                                if session.expires_at
                                else None
                            ),
                            "is_authenticated": session.is_authenticated,
                            "cookie_count": len(session.cookies),
                            "has_auth_tokens": bool(session.auth_tokens),
                        }
                    )

        return active_sessions

    def _get_session_key(self, domain: str) -> str:
        """Generate cache key for domain."""
        return f"session_{self._sanitize_domain(domain)}"

    def _sanitize_domain(self, domain: str) -> str:
        """Sanitize domain name for file storage."""
        # Remove protocol and normalize
        domain = domain.replace("https://", "").replace("http://", "")
        domain = domain.replace("/", "_").replace(":", "_")
        return hashlib.md5(domain.encode()).hexdigest()[:16]

    async def _save_session_to_file(self, domain: str, session: SessionData) -> None:
        """Save session to encrypted file."""
        session_file = self.session_dir / f"{self._sanitize_domain(domain)}.json"

        session_dict = session.to_dict()
        # Add domain to session data for cleanup purposes
        session_dict["_domain"] = domain
        json_data = json.dumps(session_dict, indent=2, default=str)

        if self.encryption_enabled:
            json_data = self._encrypt_data(json_data)

        async with aiofiles.open(session_file, "w", encoding="utf-8") as f:
            await f.write(json_data)

        # Set restrictive permissions
        os.chmod(session_file, 0o600)

    async def _load_session_from_file(self, domain: str) -> Optional[SessionData]:
        """Load session from encrypted file."""
        session_file = self.session_dir / f"{self._sanitize_domain(domain)}.json"

        if not session_file.exists():
            return None

        try:
            async with aiofiles.open(session_file, "r", encoding="utf-8") as f:
                json_data = await f.read()

            if self.encryption_enabled:
                json_data = self._decrypt_data(json_data)

            session_dict = json.loads(json_data)
            return SessionData.from_dict(session_dict)

        except Exception as e:
            logger.error(f"Failed to load session file {session_file}: {e}")
            # Delete corrupted file
            try:
                session_file.unlink()
            except (FileNotFoundError, PermissionError, OSError) as del_e:
                logger.warning(
                    f"Failed to delete corrupted session file {session_file}: {del_e}"
                )
            return None

    async def _delete_session_file(self, domain: str) -> None:
        """Delete session file."""
        session_file = self.session_dir / f"{self._sanitize_domain(domain)}.json"
        try:
            if session_file.exists():
                session_file.unlink()
        except Exception as e:
            logger.warning(f"Failed to delete session file {session_file}: {e}")

    async def _periodic_cleanup(self) -> None:
        """Periodic cleanup of expired sessions."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self.clear_expired_sessions()
            except Exception as e:
                logger.error(f"Error in periodic session cleanup: {e}")

    async def refresh_session(self, domain: str) -> bool:
        """
        Refresh session TTL if it's close to expiration.

        Args:
            domain: Domain name

        Returns:
            True if refreshed successfully
        """
        if not self.auto_refresh:
            return False

        session = await self.load_session(domain)
        if not session:
            return False

        # Check if session needs refreshing
        if session.expires_at:
            time_until_expiry = (session.expires_at - datetime.now()).total_seconds()
            if time_until_expiry > self.refresh_threshold:
                return True  # No refresh needed

        # Refresh session
        session.expires_at = datetime.now() + timedelta(seconds=self.session_ttl)
        session.update_access_time()

        return await self.save_session(domain, session)

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get session cache statistics."""
        return {
            "cached_sessions": len(self.sessions_cache),
            "valid_sessions": sum(
                1 for s in self.sessions_cache.values() if s.is_valid()
            ),
            "expired_sessions": sum(
                1 for s in self.sessions_cache.values() if s.is_expired()
            ),
            "authenticated_sessions": sum(
                1 for s in self.sessions_cache.values() if s.is_authenticated
            ),
        }
