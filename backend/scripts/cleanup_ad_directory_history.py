"""旧30天直接删除入口已退役；使用显式租户的90天持久归档命令。"""

from scripts.archive_sync_runs import main

if __name__ == "__main__":
    main()
