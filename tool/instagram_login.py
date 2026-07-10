"""
Instagram Session 保存工具
从浏览器导出的 cookies 创建 instaloader session 文件。

用法:
  python instagram_login.py <用户名> <cookies文件路径>

支持的 cookies 格式:
  - Netscape cookies.txt 格式
  - EditThisCookie JSON 格式
"""
import sys
import os
import json
import instaloader


def load_cookies_from_file(cookies_file):
    """从文件加载 cookies，自动检测格式"""
    with open(cookies_file, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    # 尝试 JSON 格式 (EditThisCookie)
    if content.startswith('[') or content.startswith('{'):
        try:
            data = json.loads(content)
            cookies = {}
            if isinstance(data, list):
                for item in data:
                    if 'instagram' in item.get('domain', ''):
                        cookies[item['name']] = item['value']
            elif isinstance(data, dict):
                for name, value in data.items():
                    cookies[name] = value
            return cookies
        except json.JSONDecodeError:
            pass

    # Netscape cookies.txt 格式
    cookies = {}
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7 and 'instagram' in parts[0]:
            cookies[parts[5]] = parts[6]
    return cookies


def main():
    if len(sys.argv) < 3:
        print("用法: python instagram_login.py <用户名> <cookies文件路径>")
        print("\n步骤:")
        print("1. 在浏览器中登录 Instagram")
        print("2. 安装浏览器扩展 EditThisCookie 或 Cookie-Editor")
        print("3. 打开 Instagram 页面，点击扩展图标，导出 cookies")
        print("4. 保存为 cookies.txt 或 cookies.json 文件")
        print("5. 运行: python instagram_login.py sakana_mochi cookies.txt")
        sys.exit(1)

    username = sys.argv[1]
    cookies_file = sys.argv[2]

    if not os.path.exists(cookies_file):
        print(f"错误: cookies 文件不存在: {cookies_file}")
        sys.exit(1)

    # 加载 cookies
    cookies = load_cookies_from_file(cookies_file)
    if not cookies:
        print("错误: 未找到 Instagram 相关的 cookie")
        print("\n请确保:")
        print("1. cookies 文件包含 Instagram 域名的 cookie")
        print("2. 包含 sessionid, ds_user_id, csrftoken 等关键 cookie")
        sys.exit(1)

    print(f"找到 {len(cookies)} 个 Instagram cookie")
    for name in cookies.keys():
        print(f"  - {name}")

    # 创建 loader 并设置 cookies
    loader = instaloader.Instaloader()

    # 设置 cookies 到 session
    for name, value in cookies.items():
        loader.context._session.cookies.set(name, value, domain='.instagram.com')

    # 验证 session
    try:
        # 尝试访问一个需要登录的页面来验证 session
        test_response = loader.context._session.get('https://www.instagram.com/api/v1/users/current/')
        if test_response.status_code == 200:
            user_data = test_response.json()
            logged_in_user = user_data.get('user', {}).get('username', 'unknown')
            print(f"\nSession 验证成功！已登录用户: {logged_in_user}")
        else:
            print(f"\n警告: Session 验证返回状态码 {test_response.status_code}")
            print("Session 可能无效或已过期，请重新导出 cookies")
            sys.exit(1)
    except Exception as e:
        print(f"\nSession 验证失败: {e}")
        sys.exit(1)

    # 保存 session
    loader.save_session_to_file(username)
    print(f"\nSession 已保存: session-{username}")
    print("请将此文件上传到服务器的 AstrBot 工作目录下。")


if __name__ == "__main__":
    main()
