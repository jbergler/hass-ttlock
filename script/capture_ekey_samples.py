"""TTLock cloud sampler.

Pulls real responses for the shared-ekey design work and redacts secrets
before writing. See `script/capture-ekey-samples` for env/usage.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://euapi.ttlock.com"
OUT_ROOT = Path("tests/fixtures/captures")

SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "accessToken",
    "refreshToken",
    "aesKeyStr",
    "lockData",
    "noKeyPwd",
    "adminPwd",
    "deletePwd",
    "lockKey",
    "unlockKey",
    "password",
}
PII_KEYS = {"remarks", "username", "senderUsername", "uid"}


def _redact(obj: Any) -> Any:
    """Walk JSON, replace secrets with a shape-preserving placeholder."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in SECRET_KEYS or k in PII_KEYS:
                out[k] = "<redacted>" if v else v
            elif k == "lockMac" and isinstance(v, str) and len(v) >= 8:
                out[k] = v[:8] + ":XX:XX:XX"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        print(f"missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return val


def _password_md5() -> str:
    plain = os.environ.get("TTLOCK_PASSWORD", "")
    pre_md5 = os.environ.get("TTLOCK_PASSWORD_MD5", "")
    if pre_md5:
        return pre_md5.strip().lower()
    if not plain:
        print("missing env: TTLOCK_PASSWORD or TTLOCK_PASSWORD_MD5", file=sys.stderr)
        sys.exit(2)
    return hashlib.md5(plain.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write(out_dir: Path, name: str, payload: Any) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(_redact(payload), indent=2, sort_keys=True) + "\n")
    print(f"  wrote {path}")


def _post(path: str, data: dict[str, Any]) -> tuple[int, Any]:
    body = urllib.parse.urlencode(
        {k: v for k, v in data.items() if v is not None}
    ).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "null")


def _get(path: str, params: dict[str, Any]) -> tuple[int, Any]:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(BASE + "/" + path.lstrip("/") + "?" + qs)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "null")


def main() -> None:
    """Run the sampler end-to-end and write redacted JSON to OUT_ROOT."""
    client_id = _env("TTLOCK_CLIENT_ID")
    client_secret = _env("TTLOCK_CLIENT_SECRET")
    username = _env("TTLOCK_USERNAME")
    password_md5 = _password_md5()
    label = os.environ.get("TTLOCK_LABEL", "account")

    out_dir = OUT_ROOT / label
    print(f"capturing into {out_dir}")

    # 1. Token.
    status, tok_json = _post(
        "/oauth2/token",
        {
            "clientId": client_id,
            "clientSecret": client_secret,
            "username": username,
            "password": password_md5,
        },
    )
    if (
        status != 200
        or not isinstance(tok_json, dict)
        or "access_token" not in tok_json
    ):
        print(f"token error ({status}): {tok_json}", file=sys.stderr)
        sys.exit(1)
    access_token = tok_json["access_token"]
    _write(out_dir, "01_oauth_token", tok_json)

    base_params = {"clientId": client_id, "accessToken": access_token}

    # 2. v3/key/list (paginated).
    all_keys: list[dict[str, Any]] = []
    page = 1
    while True:
        _, data = _post(
            "/v3/key/list",
            {
                **base_params,
                "pageNo": page,
                "pageSize": 1000,
                "date": _now_ms(),
            },
        )
        _write(out_dir, f"02_key_list_p{page}", data)
        batch = (data or {}).get("list", []) or []
        all_keys.extend(batch)
        if page >= (data or {}).get("pages", 1):
            break
        page += 1

    # 3. v3/lock/list (admin-owned) for comparison with the key-list admin entries.
    _, ll = _get(
        "/v3/lock/list",
        {**base_params, "pageNo": 1, "pageSize": 1000, "date": _now_ms()},
    )
    _write(out_dir, "03_lock_list", ll)
    owned_ids = {lk["lockId"] for lk in (ll or {}).get("list", []) or []}

    # 4. Per-lock detail + admin ekey listing. `http_status` captures 4xx so we
    #    see whether e.g. a MEMBER can read lock/detail (important for BLE key).
    for key in all_keys:
        lock_id = key["lockId"]
        user_type = key.get("userType")
        is_owner = lock_id in owned_ids
        tag = f"{lock_id}_{user_type}{'_owner' if is_owner else '_shared'}"

        d_status, d_body = _get(
            "/v3/lock/detail",
            {**base_params, "lockId": lock_id, "date": _now_ms()},
        )
        _write(
            out_dir,
            f"04_lock_detail_{tag}",
            {"http_status": d_status, "body": d_body},
        )

        k_status, k_body = _get(
            "/v3/lock/listKey",
            {
                **base_params,
                "lockId": lock_id,
                "pageNo": 1,
                "pageSize": 200,
                "orderBy": 0,
                "date": _now_ms(),
            },
        )
        _write(
            out_dir,
            f"05_lock_listKey_{tag}",
            {"http_status": k_status, "body": k_body},
        )

    # Summary for the design-verification table.
    owners = sum(1 for k in all_keys if k.get("userType") == "110301")
    shared = sum(1 for k in all_keys if k.get("userType") == "110302")
    overlap = sum(
        1
        for k in all_keys
        if k.get("userType") == "110301" and k["lockId"] in owned_ids
    )
    print(f"\nsummary: keys={len(all_keys)} admin={owners} shared={shared}")
    print(f"  lock/list owned-count={len(owned_ids)}")
    print(f"  overlap (admin keys with lockId in lock/list) = {overlap}")


if __name__ == "__main__":
    main()
