"""Entry point: python -m agentix.runtime [--port 8000] [--debug]"""

import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="agentix runtime server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--debug", action="store_true", help="Enable debugpy")
    parser.add_argument("--debug-port", type=int, default=5678, help="debugpy port")
    parser.add_argument("--debug-wait", action="store_true", help="Wait for debugger")
    args = parser.parse_args()

    if args.debug:
        import debugpy

        debugpy.listen(("0.0.0.0", args.debug_port))
        print(f"debugpy listening on 0.0.0.0:{args.debug_port}")
        if args.debug_wait:
            print("Waiting for debugger to attach...")
            debugpy.wait_for_client()
            print("Debugger attached.")

    uvicorn.run("agentix.runtime.server:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
