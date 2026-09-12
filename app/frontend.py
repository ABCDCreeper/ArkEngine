"""把构建好的前端（InnoArk-main/dist）挂在同一个端口上。

开发时前端由 vite 提供并代理 /api，两个端口各司其职；但演示与验收时只起一个进程
更省事，也避免"打开了后端端口只看到接口 404"这种困惑。没有构建产物时，根路径给出
可操作的提示而不是 404。
"""
import os

from flask import Flask, jsonify, redirect, send_from_directory

from .errors import not_found


def register_frontend(app: Flask) -> None:
    dist = app.config['FRONTEND_DIST']
    index_path = os.path.join(dist, 'index.html')

    @app.get('/')
    def frontend_root():
        if os.path.isfile(index_path):
            return send_from_directory(dist, 'index.html')
        return jsonify({
            'name': 'InnoArk 后端',
            'message': '这里只提供 /api 接口，前端尚未构建。',
            'howto': '在 InnoArk-main 下执行 yarn install && yarn build 后端即可一并提供页面；'
                     '或使用 ./start.ps1 启动开发服务器（前端 vite + 后端 Flask）。',
            'api': '/api',
        })

    if not os.path.isfile(index_path):
        return

    # 页面声明的是 favicon.svg，浏览器仍会按惯例请求 /favicon.ico，转发过去避免 404 噪音
    if os.path.isfile(os.path.join(dist, 'favicon.svg')):
        @app.get('/favicon.ico')
        def frontend_favicon():
            return redirect('/favicon.svg', code=302)

    @app.get('/<path:filename>')
    def frontend_file(filename):
        """静态文件优先，其余非接口路径回退 index.html（前端使用 history 路由）。

        找不到但带扩展名的文件按 404 返回，不能回退成 HTML：否则浏览器会把 HTML
        当成脚本或图片处理，问题更难定位。
        """
        if filename == 'api' or filename.startswith('api/'):
            # 未知接口仍然返回 JSON 404，保持接口契约
            raise not_found(f'接口不存在: {filename}')
        if os.path.isfile(os.path.join(dist, filename)):
            return send_from_directory(dist, filename)
        if os.path.splitext(filename)[1]:
            raise not_found(f'静态资源不存在: {filename}')
        return send_from_directory(dist, 'index.html')
