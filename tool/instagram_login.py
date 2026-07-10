"""
Instagram Session 保存工具
在本地运行此脚本完成登录验证，生成的 session 文件上传到服务器即可。
用法: python instagram_login.py <用户名>
"""
import sys
import instaloader


def main():
    if len(sys.argv) < 2:
        print("用法: python instagram_login.py <Instagram用户名>")
        sys.exit(1)

    username = sys.argv[1]
    loader = instaloader.Instaloader()

    print(f"正在登录 Instagram 账号: {username}")
    print("请按提示输入密码，如需安全验证请在浏览器中完成。")

    try:
        loader.login(username, input("请输入密码: "))
        loader.save_session_to_file()
        print(f"\n登录成功！session 文件已保存为: session-{username}")
        print("请将此文件上传到服务器的 AstrBot 工作目录下。")
    except Exception as e:
        print(f"\n登录失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
