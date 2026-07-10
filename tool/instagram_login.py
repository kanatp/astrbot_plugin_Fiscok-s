"""
Instagram Session 保存工具
从浏览器导出的 cookies.txt 创建 instaloader session 文件。

用法: python instagram_login.py <用户名> cookies.txt
"""
import sys
import os
import instaloader


def main():
    if len(sys.argv) < 3:
        print("用法: python instagram_login.py <用户名> <cookies.txt路径>")
        print("\n步骤:")
        print("1. 在浏览器中登录 Instagram")
        print("2. 安装浏览器扩展 EditThisCookie 或 Cookie-Editor")
        print("3. 打开 Instagram 页面，点击扩展图标，导出 cookies（Netscape 格式）")
        print("4. 保存为 cookies.txt 文件")
        print("5. 运行: python instagram_login.py sakana_mochi cookies.txt")
        sys.exit(1)

    username = sys.argv[1]
    cookies_file = sys.argv[2]

    if not os.path.exists(cookies_file):
        print(f"错误: cookies 文件不存在: {cookies_file}")
        sys.exit(1)

    loader = instaloader.Instaloader()

    # 读取 cookies 文件并提取 Instagram 相关的 cookie
    cookies = {}
    with open(cookies_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7 and 'instagram' in parts[0]:
                cookies[parts[5]] = parts[6]

    if not cookies:
        print("错误: cookies 文件中未找到 Instagram 相关的 cookie")
        sys.exit(1)

    # 设置 session 并保存
    loader.context._session.cookies.update(cookies)
    loader.save_session_to_file(username)
    print(f"Session 已保存: session-{username}")
    print("请将此文件上传到服务器的 AstrBot 工作目录下。")


if __name__ == "__main__":
    main()
