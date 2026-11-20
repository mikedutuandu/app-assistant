import os
import subprocess
import sys

HEAVY = ("pypdfium2", "pypdf", "PIL")
ENV = {
    "OPENROUTER_API_KEY": "x",
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    "S3_BUCKET": "x",
    "S3_ACCESS_KEY": "x",
    "S3_SECRET_KEY": "x",
    "PUBLIC_BASE_URL": "https://example.com",
}


def test_importing_the_app_loads_no_pdf_or_image_library():
    code = f"import sys, assistant.asgi; print([m for m in {HEAVY!r} if m in sys.modules])"
    env = {**ENV, **os.environ}
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, env=env).stdout
    assert out.strip() == "[]"
