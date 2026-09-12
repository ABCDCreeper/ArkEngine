"""口令哈希入口。

生产使用 werkzeug 默认算法（scrypt，单次约 50ms）。测试通过
PASSWORD_HASH_METHOD 注入廉价算法，避免每个用例在种子数据与登录上重复付出该成本。
校验无需注入：check_password_hash 从存储的哈希串自身解析算法。
"""
from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(password: str) -> str:
    method = current_app.config.get('PASSWORD_HASH_METHOD')
    if method:
        return generate_password_hash(password, method=method)
    return generate_password_hash(password)


def verify_password(stored: str, password: str) -> bool:
    return check_password_hash(stored, password)
