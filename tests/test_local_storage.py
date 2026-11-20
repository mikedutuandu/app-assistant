"""The local storage backend: files on disk, uploaded and read through the bot's own signed /v1/files links."""

import time
from urllib.parse import urlsplit

import pytest
from chat_api import PNG
from pydantic import ValidationError

from assistant.config import Settings
from assistant.files.signing import load_signing_key, sign
from assistant.files.storage import Storage, StorageError

KEY = "test-signing-key"


def _settings(test_settings: Settings, tmp_path, **extra) -> Settings:
    return Settings(
        openrouter_api_key="k",
        database_url="postgresql://user:pass@localhost:5432/test",
        public_base_url="http://testserver",
        data_dir=tmp_path / "data",
        files_signing_key=KEY,
        packs_dir=test_settings.packs_dir,
        run_lifespan=False,
        _env_file=None,
        **extra,
    )  # type: ignore[call-arg]


@pytest.fixture
def local(test_settings: Settings, tmp_path) -> Storage:
    return Storage(_settings(test_settings, tmp_path))


@pytest.fixture
async def local_api(api, test_settings: Settings, tmp_path):
    """The chat API (moto's S3 fixture) with the local backend in its place."""
    storage = Storage(_settings(test_settings, tmp_path))
    api.app.state.storage = storage
    api.storage = storage
    return api


def _path_query(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}"


async def _start(local_api, size: int = len(PNG)) -> dict:
    r = await local_api.start_upload(size)
    assert r.status_code == 200, r.text
    return r.json()


def test_local_is_chosen_when_neither_s3_nor_supabase_is_set(test_settings, tmp_path):
    s = _settings(test_settings, tmp_path)
    assert s.storage_backend == "local"
    assert Storage(s).upload_backend == "local"


def test_s3_and_supabase_are_still_chosen_by_their_settings(test_settings: Settings):
    assert test_settings.storage_backend == "s3"
    both = test_settings.model_copy(update={"supabase_url": "https://r.supabase.co", "supabase_service_key": "k"})
    assert both.storage_backend == "supabase"


def test_half_a_cloud_setup_is_refused(test_settings, tmp_path):
    with pytest.raises(ValidationError, match="go together"):
        _settings(test_settings, tmp_path, s3_bucket="b")
    with pytest.raises(ValidationError, match="SUPABASE_"):
        _settings(test_settings, tmp_path, supabase_url="https://r.supabase.co", supabase_service_key="k")


def test_put_get_head_and_delete_prefix(local: Storage):
    local.put("app/ws/t1/f1", b"hello", "image/png")
    local.put("app/ws/t1/f2", b"world", "image/png")
    local.put("app/ws/t2/f3", b"other", "image/png")
    assert local.get("app/ws/t1/f1") == b"hello"
    assert local.head("app/ws/t1/f1") == 5
    assert local.head("app/ws/t1/nope") is None
    assert local.delete_prefix("app/ws/t1/") == 2
    assert local.head("app/ws/t1/f1") is None
    assert local.head("app/ws/t2/f3") == 5
    assert local.delete_prefix("app/ws/t2/f3") == 1  # a whole key works as a prefix too
    assert local.delete_prefix("app/ws/") == 0


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/etc/passwd", "a//b", "a/./b", "a\\b", "", "a/b\x00c"])
def test_keys_outside_data_dir_are_refused(local: Storage, key: str):
    with pytest.raises(StorageError):
        local.put(key, b"x", "image/png")
    with pytest.raises(StorageError):
        local.head(key)
    with pytest.raises(StorageError):
        local.delete_prefix(key)


async def test_an_upload_target_stores_the_bytes_and_a_signed_get_reads_them(local_api):
    started = await _start(local_api)
    target = started["target"]
    assert target["method"] == "PUT"
    assert target["headers"] == {"content-type": "image/png"}
    assert target["url"].startswith("http://testserver/v1/files/real/ws1/")

    put = await local_api.client.put(_path_query(target["url"]), content=PNG, headers=target["headers"])
    assert put.status_code == 200
    assert (await local_api.finish(started["upload_id"], started["thread_id"])).status_code == 200

    key = f"real/ws1/{started['thread_id']}/{started['upload_id']}"
    url = local_api.storage.signed_url(key, 300)
    got = await local_api.client.get(_path_query(url))
    assert got.status_code == 200
    assert got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["access-control-allow-origin"] == "*"


async def test_a_wrong_or_missing_signature_is_403(local_api):
    target = (await _start(local_api))["target"]
    path = urlsplit(target["url"]).path
    exp = int(time.time()) + 300
    for query in (f"exp={exp}&sig={'0' * 64}", f"exp={exp}", "", f"exp=abc&sig={'0' * 64}"):
        r = await local_api.client.put(f"{path}?{query}", content=PNG, headers=target["headers"])
        assert r.status_code == 403, query
    assert local_api.storage.head(path.removeprefix("/v1/files/")) is None


async def test_an_expired_signature_is_403(local_api):
    target = (await _start(local_api))["target"]
    key = urlsplit(target["url"]).path.removeprefix("/v1/files/")
    exp = int(time.time()) - 1
    sig = sign(KEY, "PUT", key, exp)
    r = await local_api.client.put(f"/v1/files/{key}?exp={exp}&sig={sig}", content=PNG, headers=target["headers"])
    assert r.status_code == 403


async def test_a_link_works_for_its_own_method_and_key_only(local_api):
    target = (await _start(local_api))["target"]
    other = await _start(local_api)
    # The PUT link read as a GET, and the first upload's signature on the second's key: both refused.
    assert (await local_api.client.get(_path_query(target["url"]))).status_code == 403
    sig = dict(p.split("=") for p in urlsplit(target["url"]).query.split("&"))
    path = urlsplit(other["target"]["url"]).path
    r = await local_api.client.put(
        f"{path}?exp={sig['exp']}&sig={sig['sig']}", content=PNG, headers=other["target"]["headers"]
    )
    assert r.status_code == 403


async def test_a_path_that_climbs_out_is_400(local_api):
    exp = int(time.time()) + 300
    for key in ("real/%2e%2e/%2e%2e/etc/passwd", "real/ws1/..%2f..%2fx"):
        decoded = key.replace("%2e", ".").replace("%2f", "/")
        sig = sign(KEY, "GET", decoded, exp)  # even correctly signed, it is refused
        r = await local_api.client.get(f"/v1/files/{key}?exp={exp}&sig={sig}")
        assert r.status_code == 400, key


async def test_put_keeps_to_the_size_the_upload_was_made_for(local_api):
    started = await _start(local_api, size=10)
    target = started["target"]
    r = await local_api.client.put(_path_query(target["url"]), content=PNG, headers=target["headers"])
    assert r.status_code == 413
    key = f"real/ws1/{started['thread_id']}/{started['upload_id']}"
    assert local_api.storage.head(key) is None


async def test_put_keeps_to_the_content_type_the_upload_was_made_for(local_api):
    target = (await _start(local_api))["target"]
    r = await local_api.client.put(_path_query(target["url"]), content=PNG, headers={"content-type": "text/html"})
    assert r.status_code == 415


async def test_a_link_writes_once(local_api):
    target = (await _start(local_api))["target"]
    first = await local_api.client.put(_path_query(target["url"]), content=PNG, headers=target["headers"])
    again = await local_api.client.put(_path_query(target["url"]), content=PNG, headers=target["headers"])
    assert (first.status_code, again.status_code) == (200, 409)


async def test_a_signed_put_for_a_key_with_no_pending_upload_is_refused(local_api):
    key = "real/ws1/thread/nothing"
    exp = int(time.time()) + 300
    r = await local_api.client.put(
        f"/v1/files/{key}?exp={exp}&sig={sign(KEY, 'PUT', key, exp)}",
        content=PNG,
        headers={"content-type": "image/png"},
    )
    assert r.status_code == 404


async def test_a_signed_get_of_a_missing_file_is_404(local_api):
    url = local_api.storage.signed_url("real/ws1/thread/nothing", 300)
    assert (await local_api.client.get(_path_query(url))).status_code == 404


async def test_the_browser_may_call_the_links_from_any_origin(local_api):
    r = await local_api.client.options(
        "/v1/files/real/ws1/t/f",
        headers={"origin": "https://app.example", "access-control-request-method": "PUT"},
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "PUT" in r.headers["access-control-allow-methods"]
    assert "content-type" in r.headers["access-control-allow-headers"].lower()


async def test_the_generated_key_is_made_once_and_kept(pool):
    from assistant.db import migrate

    await migrate(pool)
    first = await load_signing_key(pool)
    assert len(first) == 64
    assert await load_signing_key(pool) == first


def test_a_get_url_is_valid_for_the_seconds_asked(local: Storage):
    url = local.signed_url("a/b/c", 300)
    query = dict(p.split("=") for p in urlsplit(url).query.split("&"))
    assert abs(int(query["exp"]) - (time.time() + 300)) < 5
    assert local.object_url("demo/ws/").startswith("http://testserver/v1/files/demo/ws/")


async def test_a_non_ascii_signature_is_403_not_500(local_api):
    target = (await _start(local_api))["target"]
    path = urlsplit(target["url"]).path
    exp = int(time.time()) + 300
    for method in ("put", "get"):
        r = await getattr(local_api.client, method)(f"{path}?exp={exp}&sig=%C3%A9", headers=target["headers"])
        assert r.status_code == 403, method


def test_a_folder_prefix_never_matches_a_sibling(local: Storage):
    local.put("a/bc", b"x", "image/png")
    assert local.delete_prefix("a/b/") == 0
    assert local.head("a/bc") == 1


def test_a_symlink_out_of_data_dir_is_refused(local: Storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"s")
    local.put("a/f", b"x", "image/png")
    (local.object_path("a/f").parent / "link").symlink_to(outside)
    with pytest.raises(StorageError):
        local.get("a/link/secret")
    with pytest.raises(StorageError):
        local.object_path("a/link/secret")
