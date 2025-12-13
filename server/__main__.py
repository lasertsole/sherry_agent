from config import API_HOST, API_PORT

if __name__ == "__main__":
    print(f"🚀 服务器启动中... 地址：http://{API_HOST}:{API_PORT}")

    # 导入以注册所有路由和处理器
    from .trigger import app
    app.start(host=API_HOST, port=API_PORT)