"""`python -m voiceops` starts the mock legacy PMS (port 8001) and the agent platform (port 8000)."""
from __future__ import annotations

import os
import threading
import time

import httpx
import uvicorn


def main() -> None:
    pms_port = int(os.environ.get("PMS_PORT", "8001"))
    port = int(os.environ.get("PORT", "8000"))
    os.environ.setdefault("PMS_BASE_URL", f"http://127.0.0.1:{pms_port}")
    pms = uvicorn.Server(uvicorn.Config("voiceops.legacy_pms.app:app", host="127.0.0.1", port=pms_port, log_level="warning", access_log=False))
    threading.Thread(target=pms.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{pms_port}/docs", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    from voiceops.config import settings
    from voiceops.server import create_app
    st = settings()
    print(f"VoiceOps console: http://localhost:{port}   model provider: {st.model_provider}"
          + (f" ({st.model}, effort={st.effort})" if st.model_provider == "claude" else " (offline, no API key needed)"))
    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"), port=port, log_level="info")


if __name__ == "__main__":
    main()
