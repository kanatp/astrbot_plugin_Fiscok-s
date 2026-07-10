"""
Instagram Session 保存工具
从浏览器 EditThisCookie 导出的 JSON 文件创建 instaloader session。

用法: python instagram_login.py <用户名> <cookies.json>
"""
import sys
import os
import json
import instaloader


def main():
    if len(sys.argv) < 3:
        print("用法: python instagram_login.py <用户名> <cookies.json>")
        print("\n步骤:")
        print("1. 在浏览器中登录 Instagram")
        print("2. 安装 EditThisCookie 扩展")
        print("3. 打开 Instagram 页面，点击扩展导出 cookies")
        print("4. 保存为 cookies.json")
        print("5. 运行: python instagram_login.py sakana_mochi cookies.json")
        sys.exit(1)

    username = sys.argv[1]
    cookies_file = sys.argv[2]

    if not os.path.exists(cookies_file):
        print(f"错误: 文件不存在: {cookies_file}")
        sys.exit(1)

    with open(cookies_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    cookies = {}
    for item in data:
        if 'instagram' in item.get('domain', ''):
            cookies[item['name']] = item['value']

    if 'sessionid' not in cookies:
        print("错误: 缺少 sessionid cookie，请确认已登录 Instagram 后重新导出")
        sys.exit(1)

    loader = instaloader.Instaloader()
    for name, value in cookies.items():
        loader.context._session.cookies.set(name, value, domain='.instagram.com')

    loader.save_session_to_file(username)
    print(f"Session 已保存: session-{username}")
    print("请将此文件上传到服务器的 AstrBot 工作目录下。")


if __name__ == "__main__":
    main()
