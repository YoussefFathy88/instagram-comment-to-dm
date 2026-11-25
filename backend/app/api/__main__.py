"""Start the control board on 127.0.0.1 only (never exposed to the network)."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.api.main:app", host="127.0.0.1", port=int(os.environ.get("COMMENT_DM_PORT", "8765")), log_level="warning")
