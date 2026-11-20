from pathlib import Path

import pytest

from assistant.config import Settings


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        openrouter_api_key="test-openrouter-key",
        database_url="postgresql://user:pass@localhost:5432/test",
        s3_endpoint=None,
        s3_bucket="test-bucket",
        s3_access_key="test-access",
        s3_secret_key="test-secret",
        packs_dir=tmp_path / "apps",
        public_base_url="http://testserver",
    )
