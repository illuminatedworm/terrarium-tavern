import argparse
import asyncio
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Terrarium Tavern — powered by KohakuTerrarium",
        epilog="KohakuTerrarium: https://github.com/Kohaku-Lab/KohakuTerrarium",
    )
    parser.add_argument("--port", type=int, default=51273)
    parser.add_argument("--data", default="data")
    parser.add_argument("--desktop", action="store_true", help="打开支持隔离卡片脚本的桌面窗口")
    parser.add_argument("--dev", action="store_true", help="启动受控开发窗口、Vite 5173 与后端 51273")
    parser.add_argument("--backup", help="创建完整备份 ZIP 后退出")
    parser.add_argument("--restore", help="从备份 ZIP 恢复到 --data 指定的新空目录")
    args = parser.parse_args()
    if args.dev and args.port != 51273:
        parser.error("--dev 使用固定后端端口 51273")
    if args.backup or args.restore:
        from tavern.storage.backup import backup, restore
        from tavern.storage.store import Store

        if args.restore:
            restore(Path(args.restore), Path(args.data))
            print("恢复完成")
        else:
            backup(Store(Path(args.data)), Path(args.backup))
            print("备份完成")
        return
    from tavern.desktop_browser import main as check_browser

    # Desktop CLI preflight happens before opening the store. Android creates its
    # app directly and keeps using its native WebView backend.
    check_browser()
    os.environ["TAVERN_DATA"] = args.data
    import uvicorn

    from tavern.application.api import cancel_producers, create_app

    app = create_app(args.data)
    ready = asyncio.Event()

    class WindowServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # Resource observers exist even without a running story. Stop their
            # producers before uvicorn drains SSE on Ctrl+C / termination signals.
            cancel_producers(app)
            super().handle_exit(sig, frame)

        async def startup(self, sockets=None):
            await super().startup(sockets)
            if self.started:
                ready.set()

    server = WindowServer(uvicorn.Config(app, host="127.0.0.1", port=args.port, timeout_graceful_shutdown=5))

    def shutdown():
        cancel_producers(app)
        server.should_exit = True

    app.state.shutdown_callback = shutdown
    if args.desktop or args.dev:
        from tavern.desktop import serve_window, with_dev_frontend

        async def run():
            await serve_window(
                server,
                ready,
                shutdown,
                Path(args.data).resolve() / "desktop-profile",
                "http://127.0.0.1:" + str(5173 if args.dev else args.port),
            )

        asyncio.run(with_dev_frontend(run) if args.dev else run())
    else:
        server.run()


if __name__ == "__main__":
    main()
