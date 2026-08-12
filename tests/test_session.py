from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from telethon.crypto import AuthKey  # type: ignore[import-untyped]
from telethon.sessions import StringSession  # type: ignore[import-untyped]

from xerxes_tg import session


class SessionCompatibilityTests(unittest.TestCase):
    def test_load_session_falls_back_to_legacy_keyring_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            encrypted_path = state_dir / "session.enc"
            key_path = state_dir / "session.key"
            legacy_path = state_dir / "legacy.session"

            source = StringSession()
            source.set_dc(2, "149.154.167.51", 443)
            source.auth_key = AuthKey(os.urandom(256))
            serialized = source.save()

            keyring_key = Fernet.generate_key()
            encrypted_path.write_bytes(Fernet(keyring_key).encrypt(serialized.encode()))
            key_path.write_bytes(Fernet.generate_key())

            with (
                patch.object(session, "_ENC_PATH", encrypted_path),
                patch.object(session, "_KEY_PATH", key_path),
                patch.object(session, "_LEGACY_SQLITE", legacy_path),
                patch.object(session, "_get_legacy_keyring_key", return_value=keyring_key),
            ):
                loaded = session.load_session()

            self.assertEqual(loaded.save(), serialized)


if __name__ == "__main__":
    unittest.main()
