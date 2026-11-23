"""Run with: python -m naviblend"""
import logging
import os

import uvicorn

from .web import create_app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "4545")),
                proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
