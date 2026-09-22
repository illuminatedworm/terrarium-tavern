import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Terrarium Tavern — powered by KohakuTerrarium")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default="data")
    parser.add_argument("--backup", help="创建完整备份 ZIP 后退出")
    parser.add_argument("--restore", help="从备份 ZIP 恢复到 --data 指定的新空目录")
    args = parser.parse_args()
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
    os.environ["TAVERN_DATA"] = args.data
    import uvicorn

    from tavern.application.api import create_app

    app = create_app(args.data)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port))

    def shutdown():
        for task in app.state.service.tasks.values():
            if not task.done():
                task.cancel()
        server.should_exit = True

    app.state.shutdown_callback = shutdown
    server.run()


if __name__ == "__main__":
    main()
