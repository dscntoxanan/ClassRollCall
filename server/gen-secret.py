#!/usr/bin/env python3
"""生成 rollcall-api 需要的 secret.json。

用法：
    python3 gen-secret.py "你的管理Token" [同步密钥]
    python3 gen-secret.py                 # 两者都随机生成
"""

import hashlib
import json
import os
import secrets
import sys


def main() -> None:
    if len(sys.argv) > 1:
        token = sys.argv[1]
    else:
        token = secrets.token_urlsafe(24)
        print("已随机生成管理 Token（请自己保存好）：", token)

    sync_key = sys.argv[2] if len(sys.argv) > 2 else secrets.token_hex(16)

    data = {
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "sync_key": sync_key,
        "cookie_secret": secrets.token_hex(24),
    }

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)

    print("已写入", path)
    print("sync_key（填到客户端 SYNC_KEY）：", sync_key)


if __name__ == "__main__":
    main()
