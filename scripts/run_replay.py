"""Start the historical workspace on localhost without starting a broker."""
from pathlib import Path
import argparse
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    import uvicorn
    from src.utils.config import get_env_value
    from src.web.replay import create_replay_app
    from src.web.windows_asyncio import install_web_event_loop_policy

    parser = argparse.ArgumentParser(description="Breakout Lab: historical Scanner 2 study replay")
    parser.add_argument("--port", type=int, default=int(get_env_value("REPLAY_PORT", "8765")))
    args = parser.parse_args()
    install_web_event_loop_policy()
    # Acquire the port before lifespan performs database recovery. A duplicate
    # launch must not mark an active scanner worker as interrupted.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", args.port))
        except OSError:
            raise SystemExit(f"Port {args.port} is already in use. Open http://localhost:{args.port} or choose another --port.") from None
        listener.listen(128)
        listener.setblocking(False)
        print(f"Breakout Lab: http://localhost:{args.port}", flush=True)
        config = uvicorn.Config(create_replay_app(), host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
        uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    main()
