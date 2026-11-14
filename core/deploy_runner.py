"""本机进程部署的启动器: python -m core.deploy_runner <flask_app 目录> <绑定地址> <端口>

不需要 Docker。载入生成工程的 app.py, 优先用 waitress(多线程生产级), 没装就用 werkzeug 多线程。
与平台进程脱离(独立会话), 平台重启不影响已部署站点; 进程命令行带 deploy_runner 标记, 供存活校验。
"""
import importlib.util
import os
import sys


def main(argv):
    app_dir, host, port = argv[1], argv[2], int(argv[3])
    os.chdir(app_dir)
    sys.path.insert(0, app_dir)
    os.environ.setdefault("PORT", str(port))
    spec = importlib.util.spec_from_file_location("clone_site_app", os.path.join(app_dir, "app.py"))
    if spec is None or spec.loader is None:
        raise SystemExit("app.py 不存在")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["clone_site_app"] = mod
    spec.loader.exec_module(mod)
    app = mod.app
    print(f" * clonjurer deploy: http://{host}:{port}", flush=True)
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8, ident="clonjurer")
    except ImportError:
        from werkzeug.serving import run_simple
        run_simple(host, port, app, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main(sys.argv)
