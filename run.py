"""开发入口：python run.py（默认 http://127.0.0.1:5000）。

可用环境变量覆盖：HOST / PORT / FLASK_DEBUG。
"""
import os

from app import create_app

app = create_app()

if __name__ == '__main__':
    app.run(
        host=os.environ.get('HOST', '127.0.0.1'),
        port=int(os.environ.get('PORT', '5000')),
        debug=os.environ.get('FLASK_DEBUG', '1') == '1',
    )
