"""Entry point: python -m hnix [--host 0.0.0.0] [--port 8000]"""

import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="hnix runtime server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    args = parser.parse_args()

    uvicorn.run("hnix.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
